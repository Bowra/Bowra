from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.language_context import LanguageContext
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.session_store import SessionProtocolManagedError, SessionStore
from bowhead_core.terminal_protocol import (
    ActorScope,
    AdmissionContext,
    AuthFenceEpoch,
    AuthFenceResolveContext,
    AuthFenceScope,
    AuthFenceScopeKind,
    AuthFenceVector,
    EventPublishMode,
    ExecutionGenerationContext,
    GenerationClaimContext,
    GenerationState,
    PhaseScope,
    PostTerminalAuxiliaryContext,
    RootDurability,
    TerminalAdmissionRequest,
    TerminalProtocolFutureVersionError,
    TerminalProtocolMalformedError,
    TerminalScope,
    TerminalStatus,
    canonical_digest_v1,
    canonical_json_v1,
)
from bowhead_core.terminal_protocol_store import (
    AdmissionCode,
    AdmissionDisposition,
    GenerationClaimCode,
    GenerationClaimDisposition,
    SessionProtocolFacts,
    TerminalProtocolFaultPoint,
    TerminalProtocolStore,
    TerminalReadContext,
    WebChatActionFutureVersionError,
    WebChatActionMalformedError,
    _idempotent_event_identity,
    _read_start_admission_replay,
    _typed_execution_context_matches,
    open_terminal_protocol_connection,
)

_NOW = datetime(2026, 8, 3, 4, 5, 6, tzinfo=UTC)


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = tmp_path / "bowhead.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    upgrade(f"sqlite+aiosqlite:///{path}", target="core")
    return str(path)


def _context(*, session_id: str = "s_1", turn_id: str = "t_1") -> TerminalReadContext:
    return TerminalReadContext(
        team_id="team_1",
        session_id=session_id,
        turn_id=turn_id,
        viewer="viewer_1",
        auth_fence_vector_digest="auth_digest_1",
    )


def _insert_base(connection: sqlite3.Connection, *, session_id: str = "s_1") -> None:
    connection.execute(
        "INSERT OR IGNORE INTO teams(id,name,status,created_at) VALUES ('team_1','Team','active','t')",
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO workspaces(id,team_id,name,status,created_at,updated_at,revision)
        VALUES ('ws_1','team_1','Space','active','t','t',1)
        """,
    )
    connection.execute(
        """
        INSERT INTO sessions(id,workspace_id,user_id,title,started_at,last_active_at,status,cost_usd,tokens_total,intents)
        VALUES (?,'ws_1','u_1','Session','2026-08-01T00:00:00Z','2026-08-01T00:00:00Z','active',0,0,'[]')
        """,
        (session_id,),
    )
    for entry in _auth_vector(session_id).entries:
        connection.execute(
            """
            INSERT OR IGNORE INTO terminal_auth_scope_epochs(
              scope_kind,team_id,scope_ref,epoch,row_version,updated_at
            ) VALUES (?,?,?,1,1,'t')
            """,
            (
                entry.scope.scope_kind.value,
                entry.scope.team_id,
                entry.scope.scope_ref,
            ),
        )


def _insert_home_prerequisites(
    connection: sqlite3.Connection,
    *,
    session_id: str,
) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO teams(id,name,status,created_at) VALUES ('team_1','Team','active','t')",
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO workspaces(id,team_id,name,status,created_at,updated_at,revision)
        VALUES ('ws_1','team_1','Space','active','t','t',1)
        """,
    )
    for entry in _auth_vector(session_id).entries:
        connection.execute(
            """
            INSERT OR IGNORE INTO terminal_auth_scope_epochs(
              scope_kind,team_id,scope_ref,epoch,row_version,updated_at
            ) VALUES (?,?,?,1,1,'t')
            """,
            (
                entry.scope.scope_kind.value,
                entry.scope.team_id,
                entry.scope.scope_ref,
            ),
        )


def _auth_vector(session_id: str = "s_1") -> AuthFenceVector:
    return AuthFenceVector.build(
        (
            AuthFenceEpoch(
                AuthFenceScope(
                    AuthFenceScopeKind.TEAM_MEMBERSHIP,
                    "team_1",
                    "team_1",
                ),
                1,
            ),
            AuthFenceEpoch(
                AuthFenceScope(
                    AuthFenceScopeKind.RESOURCE_GRANT,
                    "team_1",
                    "team_1",
                ),
                1,
            ),
            AuthFenceEpoch(
                AuthFenceScope(
                    AuthFenceScopeKind.SESSION_VISIBILITY,
                    "team_1",
                    session_id,
                ),
                1,
            ),
            AuthFenceEpoch(
                AuthFenceScope(
                    AuthFenceScopeKind.USER_ACCOUNT,
                    "__global__",
                    "u_1",
                ),
                1,
            ),
        )
    )


def _insert_writer_lease(
    connection: sqlite3.Connection,
    *,
    owner: str = "gateway_1",
    epoch: int = 7,
    admission_state: str = "open",
    expires_at: datetime | None = None,
) -> None:
    expiry = (expires_at or (_NOW + timedelta(minutes=5))).isoformat().replace("+00:00", "Z")
    connection.execute(
        """
        INSERT INTO terminal_protocol_writer_lease(
          lease_name,owner_instance_id,epoch,admission_state,admission_epoch,
          rollout_config_generation,lease_expires_at,heartbeat_at
        ) VALUES ('v1_writer',?,?,?,3,'rollout_1',?,?)
        """,
        (owner, epoch, admission_state, expiry, _NOW.isoformat()),
    )


def _admission_request(
    *,
    request_id: str = "req_1",
    turn_id: str = "t_1",
    session_id: str = "s_1",
    user_text: str = "hello",
    execution_intent: dict[str, object] | None = None,
    allow_steer: bool = True,
    writer_instance_id: str = "gateway_1",
    writer_lease_epoch: int = 7,
    writer_admission_epoch: int = 3,
    auth_fence_vector_digest: str | None = None,
    user_source: ChatTurnUserSource | None = None,
    create_session_if_missing: bool = False,
    require_existing_v1_history: bool = False,
    expected_execution_generation: int | None = None,
    user_message_id: str | None = None,
) -> TerminalAdmissionRequest:
    return TerminalAdmissionRequest(
        context=AdmissionContext(
            team_id="team_1",
            session_id=session_id,
            request_id=request_id,
            surface="web",
            cohort="bucket=1;config=rollout_1",
            actor="u_1",
            writer_instance_id=writer_instance_id,
            writer_lease_epoch=writer_lease_epoch,
            writer_admission_epoch=writer_admission_epoch,
            rollout_config_generation="rollout_1",
            auth_fence_vector_digest=(
                auth_fence_vector_digest if auth_fence_vector_digest is not None else _auth_vector(session_id).auth_fence_vector_digest
            ),
        ),
        turn_id=turn_id,
        user_text=user_text,
        execution_intent=execution_intent or {"text": user_text},
        presentation_locale="zh-CN",
        message_catalog_version=1,
        allow_steer=allow_steer,
        user_source=user_source,
        create_session_if_missing=create_session_if_missing,
        require_existing_v1_history=require_existing_v1_history,
        expected_execution_generation=expected_execution_generation,
        user_message_id=user_message_id,
    )


def _typed_admission_request(
    *,
    request_id: str = "act_start_0123456789abcdef0123456789abcdef",
    turn_id: str = "t_0123456789abcdef0123456789abcdef",
    session_id: str = "s_0123456789abcdef0123456789abcdef",
    user_text: str = "hello",
    attachment_ids: tuple[str, ...] = (),
    attachment_refs: tuple[dict[str, object], ...] = (),
    parts: tuple[dict[str, object], ...] | None = None,
    create_session_if_missing: bool = False,
    require_existing_v1_history: bool = False,
    writer_admission_epoch: int = 3,
) -> TerminalAdmissionRequest:
    source = ChatTurnUserSource(
        turn_id=turn_id,
        session_id=session_id,
        user_id="u_1",
        workspace_id="ws_1",
        user_text=user_text,
        attachment_ids=attachment_ids,
        attachment_refs=attachment_refs,
        parts=parts,
    )
    return _admission_request(
        request_id=request_id,
        turn_id=turn_id,
        session_id=session_id,
        user_text=user_text,
        execution_intent={
            "active_datarc_project": None,
            "attachment_refs": list(attachment_refs),
            "attachments": list(attachment_ids),
            "language_context": None,
            "member_uid": "",
            "parts": list(parts) if parts is not None else None,
            "planning_mode": None,
            "preferred_model": None,
            "preferred_provider": None,
            "text": user_text,
            "workspace_id": "ws_1",
        },
        allow_steer=False,
        user_source=source,
        create_session_if_missing=create_session_if_missing,
        require_existing_v1_history=require_existing_v1_history,
        writer_admission_epoch=writer_admission_epoch,
    )


def _s1_store(
    db_path: str,
    *,
    fault: TerminalProtocolFaultPoint | None = None,
) -> TerminalProtocolStore:
    def inject(point: TerminalProtocolFaultPoint) -> None:
        if point is fault:
            raise RuntimeError(f"injected:{point}")

    return TerminalProtocolStore(
        db_path,
        _clock=lambda: _NOW,
        _fault_injector=inject if fault is not None else None,
    )


def _claim_context(*, owner: str = "gateway_1", row_version: int = 1) -> GenerationClaimContext:
    return GenerationClaimContext(
        scope=TerminalScope(team_id="team_1", session_id="s_1", turn_id="t_1", generation=1),
        expected_generation_row_version=row_version,
        expected_session_claim_epoch=0,
        writer_lease_epoch=7,
        owner_instance_id=owner,
    )


async def _start_execution(store: TerminalProtocolStore) -> ExecutionGenerationContext:
    admitted = await store.admit_or_steer_v1(_admission_request())
    assert admitted.disposition is AdmissionDisposition.ADMITTED
    claimed = await store.claim_generation(_claim_context())
    assert claimed.context is not None
    return await store.mark_execution_started(claimed.context)


def _protocol_counts(db_path: str) -> dict[str, int]:
    with sqlite3.connect(db_path) as connection:
        return {
            "turns": int(connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0]),
            "turn_generations": int(connection.execute("SELECT COUNT(*) FROM turn_generations").fetchone()[0]),
            "turn_events": int(connection.execute("SELECT COUNT(*) FROM turn_events").fetchone()[0]),
            "turn_effect_attempts": int(connection.execute("SELECT COUNT(*) FROM turn_effect_attempts").fetchone()[0]),
            "turn_terminal_receipts": int(connection.execute("SELECT COUNT(*) FROM turn_terminal_receipts").fetchone()[0]),
        }


def _insert_v1_open(
    connection: sqlite3.Connection,
    *,
    session_id: str = "s_1",
    turn_id: str = "t_1",
    seq: int = 1,
) -> None:
    connection.execute(
        """
        INSERT INTO turns(
          id,session_id,seq,user_text,status,started_at,
          terminal_protocol_version,terminal_ingress_surface,terminal_admission_cohort,
          protocol_admitted_at,current_generation,generation_transition_epoch,
          event_sequence_next,delivery_fence_epoch,presentation_locale,message_catalog_version
        ) VALUES (?, ?, ?, 'hello', 'running', 't', 1, 'web', 'bucket/config', 't', 1, 1, 1, 0, 'zh-CN', 1)
        """,
        (turn_id, session_id, seq),
    )
    connection.execute(
        """
        INSERT INTO turn_generations(
          turn_id,generation,protocol_version,kind,execution_intent_id,
          execution_intent_digest,opened_at,state
        ) VALUES (?,1,1,'execution','intent_1','intent_digest_1','t','opened')
        """,
        (turn_id,),
    )


@pytest.mark.asyncio
async def test_legacy_submission_steer_cannot_write_active_v1_session(
    db_path: str,
) -> None:
    session_id = "s_v1_submission_steer"
    turn_id = "t_v1_submission_steer"
    now = _NOW.isoformat()
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id=session_id)
        _insert_v1_open(
            connection,
            session_id=session_id,
            turn_id=turn_id,
        )
        connection.execute(
            "UPDATE sessions SET active_turn_id=?,active_turn_protocol_version=1,active_turn_generation=1,active_turn_claim_epoch=0 WHERE id=?",
            (turn_id, session_id),
        )
        connection.execute(
            "INSERT INTO chat_auth_session_generations("
            "user_id,auth_session_generation_hash,state,created_at,invalidated_at"
            ") VALUES ('u_1',?,'active',?,NULL)",
            ("0" * 64, now),
        )
        connection.commit()

    store = SessionStore(db_path)
    common = {
        "team_id": "team_1",
        "user_id": "u_1",
        "client_submission_id": "018f0cc3-e4a2-7b21-a109-001122334599",
        "workspace_id": "ws_1",
        "requested_session_id": session_id,
        "submission_kind": "steer",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "request_fingerprint": "a" * 64,
        "accepted_request_fingerprints": ("a" * 64,),
        "fingerprint_key_id": "f" * 64,
        "auth_session_generation_hash": "0" * 64,
    }
    claim = await store.claim_chat_submission(**common)
    assert claim.kind == "claimed"

    with pytest.raises(SessionProtocolManagedError):
        await store.finalize_steer_chat_submission(
            team_id="team_1",
            user_id="u_1",
            client_submission_id=common["client_submission_id"],
            workspace_id="ws_1",
            requested_session_id=session_id,
            expected_parent_turn_id=turn_id,
            expected_execution_generation=1,
            text="must not enter the legacy buffer",
            attachment_ids=[],
            submission_contract_version="chat-submit.v1",
            model_control_batch="a",
            request_fingerprint="a" * 64,
            lease_owner=claim.lease_owner,
            lease_version=claim.lease_version,
        )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT steer_buffer_json FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone() == ("[]",)
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_submission_receipts WHERE client_submission_id=?",
            (common["client_submission_id"],),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_submission_inflight WHERE client_submission_id=?",
            (common["client_submission_id"],),
        ).fetchone() == (1,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("protocol_case", "expected_error"),
    [
        ("future", TerminalProtocolFutureVersionError),
        ("malformed", TerminalProtocolMalformedError),
    ],
)
async def test_legacy_submission_steer_fails_closed_on_invalid_parent_protocol(
    db_path: str,
    protocol_case: str,
    expected_error: type[Exception],
) -> None:
    session_id = f"s_{protocol_case}_submission_steer"
    turn_id = f"t_{protocol_case}_submission_steer"
    submission_id = "018f0cc3-e4a2-7b21-a109-00112233459a" if protocol_case == "future" else "018f0cc3-e4a2-7b21-a109-00112233459b"
    now = _NOW.isoformat()
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id=session_id)
        if protocol_case == "future":
            connection.execute(
                "INSERT INTO turns("
                "id,session_id,seq,user_text,status,started_at,"
                "terminal_protocol_version"
                ") VALUES (?, ?, 1, 'future', 'running', 't', 2)",
                (turn_id, session_id),
            )
        else:
            _insert_v1_open(
                connection,
                session_id=session_id,
                turn_id=turn_id,
            )
            connection.execute(
                "DROP TRIGGER IF EXISTS sessions_terminal_active_reference_update",
            )
        connection.execute(
            "UPDATE sessions SET active_turn_id=? WHERE id=?",
            (turn_id, session_id),
        )
        connection.execute(
            "INSERT INTO chat_auth_session_generations("
            "user_id,auth_session_generation_hash,state,created_at,invalidated_at"
            ") VALUES ('u_1',?,'active',?,NULL)",
            ("0" * 64, now),
        )
        connection.commit()

    store = SessionStore(db_path)
    common = {
        "team_id": "team_1",
        "user_id": "u_1",
        "client_submission_id": submission_id,
        "workspace_id": "ws_1",
        "requested_session_id": session_id,
        "submission_kind": "steer",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "request_fingerprint": "b" * 64,
        "accepted_request_fingerprints": ("b" * 64,),
        "fingerprint_key_id": "f" * 64,
        "auth_session_generation_hash": "0" * 64,
    }
    claim = await store.claim_chat_submission(**common)
    assert claim.kind == "claimed"

    with pytest.raises(expected_error):
        await store.finalize_steer_chat_submission(
            team_id="team_1",
            user_id="u_1",
            client_submission_id=submission_id,
            workspace_id="ws_1",
            requested_session_id=session_id,
            expected_parent_turn_id=turn_id,
            expected_execution_generation=1,
            text="must stay fenced",
            attachment_ids=[],
            submission_contract_version="chat-submit.v1",
            model_control_batch="a",
            request_fingerprint="b" * 64,
            lease_owner=claim.lease_owner,
            lease_version=claim.lease_version,
        )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT steer_buffer_json FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone() == ("[]",)
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_submission_receipts WHERE client_submission_id=?",
            (submission_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_submission_inflight WHERE client_submission_id=?",
            (submission_id,),
        ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_exact_legacy_turn_replay_cannot_cross_v1_session_history(
    db_path: str,
) -> None:
    session_id = "s_legacy_then_v1"
    legacy_turn_id = "t_legacy_history"
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id=session_id)
        connection.commit()
    store = SessionStore(db_path)
    await store.record_turn(
        turn_id=legacy_turn_id,
        session_id=session_id,
        user_text="legacy request",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turns SET status='completed',ended_at='t' WHERE id=?",
            (legacy_turn_id,),
        )
        connection.execute(
            "UPDATE sessions SET active_turn_id=NULL WHERE id=?",
            (session_id,),
        )
        _insert_v1_open(
            connection,
            session_id=session_id,
            turn_id="t_v1_history",
            seq=2,
        )
        connection.commit()

    with pytest.raises(SessionProtocolManagedError):
        await store.record_turn(
            turn_id=legacy_turn_id,
            session_id=session_id,
            user_text="legacy request",
        )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT id,status,terminal_protocol_version FROM turns WHERE session_id=? ORDER BY seq",
            (session_id,),
        ).fetchall() == [
            (legacy_turn_id, "completed", None),
            ("t_v1_history", "running", 1),
        ]
        assert connection.execute(
            "SELECT active_turn_id FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone() == (None,)


@pytest.mark.asyncio
async def test_terminal_protocol_store_initialize_only_validates_schema(db_path: str, tmp_path: Path) -> None:
    await TerminalProtocolStore(db_path).initialize()
    async with open_terminal_protocol_connection(db_path) as connection:
        async with connection.execute("PRAGMA synchronous") as cursor:
            assert await cursor.fetchone() == (2,)
        async with connection.execute("PRAGMA foreign_keys") as cursor:
            assert await cursor.fetchone() == (1,)

    bare = tmp_path / "bare.db"
    sqlite3.connect(bare).close()
    with pytest.raises(RuntimeError, match="migrate upgrade head"):
        await TerminalProtocolStore(bare).initialize()
    with sqlite3.connect(bare) as connection:
        assert connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone() == (0,)


@pytest.mark.asyncio
async def test_terminal_protocol_reader_preserves_legacy_shape(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        connection.execute(
            "INSERT INTO turns(id,session_id,seq,user_text,status,started_at) VALUES ('t_1','s_1',1,'legacy','running','t')",
        )
        connection.commit()

    summary = await TerminalProtocolStore(db_path).read_protocol_summary(_context())
    assert summary.version is None
    assert summary.current_generation is None
    assert summary.current_receipt is None


@pytest.mark.asyncio
async def test_terminal_protocol_reader_reads_open_generation_without_guessing_terminal(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_v1_open(connection)
        connection.commit()

    summary = await TerminalProtocolStore(db_path).read_protocol_summary(_context())
    assert summary.version == 1
    assert summary.current_generation == 1
    assert summary.current_generation_kind == "execution"
    assert summary.generation_transition_epoch == 1
    assert summary.current_generation_state is GenerationState.OPENED
    assert summary.last_terminal_generation is None
    assert summary.current_receipt is None
    assert summary.recovery is None
    assert summary.presentation_locale == "zh-CN"


@pytest.mark.asyncio
async def test_terminal_protocol_reader_reads_committed_receipt_pointer(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_base(connection)
        _insert_v1_open(connection)
        connection.execute(
            """
            INSERT INTO turn_terminal_candidates(
              candidate_id,turn_id,generation,candidate_origin,reserved_receipt_id,reserved_terminal_event_id,
              finalization_epoch_at_prepare,primary_status,primary_message_key,closing_accepted_through,
              persisted_event_highwater,terminal_event_counts_by_variant_json,candidate_projection_digest,
              canonicalization_version,state,session_disposition_plan_json,session_disposition_plan_digest,
              projection_variant_digests_json,projection_extensions_json,created_at,prepared_at,committed_at
            ) VALUES ('ttc_1','t_1',1,'normal','trc_1','evt_done',1,'completed','terminal.completed',0,0,
                      '{}','dc_1',1,'committed','{}','plan_1','{}','{}','t','t','t')
            """,
        )
        connection.execute(
            """
            INSERT INTO turn_terminal_candidate_variants(
              candidate_id,variant_key,outcome_variant,session_variant,status,message_key,
              final_answer_mode,final_answer_event_id,final_answer_digest,
              required_manifest_template_digest,session_mutation_template_digest,variant_digest
            ) VALUES ('ttc_1','primary_none','primary','none','completed','terminal.completed',
                      'precommitted','evt_answer','answer_digest','manifest_template','session_template','variant_digest')
            """,
        )
        connection.execute(
            """
            INSERT INTO turn_terminal_receipts(
              receipt_id,turn_id,generation,candidate_id,protocol_version,status,message_key,
              final_answer_mode,final_answer_event_id,selected_variant_key,selected_outcome_variant,
              selected_session_variant,terminal_event_id,first_event_seq,last_event_seq,
              persisted_event_highwater,required_event_manifest_json,candidate_projection_digest,
              selected_projection_digest,selected_manifest_digest,selected_session_mutation_digest,
              projection_extensions_json,committed_at
            ) VALUES ('trc_1','t_1',1,'ttc_1',1,'completed','terminal.completed',
                      'precommitted','evt_answer','primary_none','primary','none','evt_done',1,2,0,
                      '[]','dc_1','dp_1','dm_1','ds_1','{}','t')
            """,
        )
        connection.execute(
            """
            UPDATE turn_generations
            SET state='closed',closing_accepted_through=0,terminal_candidate_id='ttc_1',
                terminal_receipt_id='trc_1',closed_at='t'
            WHERE turn_id='t_1' AND generation=1
            """,
        )
        connection.execute(
            """
            UPDATE turns
            SET status='completed',last_terminal_generation=1,current_terminal_receipt_id='trc_1'
            WHERE id='t_1'
            """,
        )
        connection.commit()

    summary = await TerminalProtocolStore(db_path).read_protocol_summary(_context())
    assert summary.current_generation_state is GenerationState.CLOSED
    assert summary.last_terminal_generation == 1
    assert summary.current_receipt is not None
    assert summary.current_receipt.receipt_id == "trc_1"
    assert summary.current_receipt.status is TerminalStatus.COMPLETED
    assert summary.current_receipt.terminal_event_id == "evt_done"
    with pytest.raises(TerminalProtocolMalformedError, match="event highwater|terminal event dependency"):
        await TerminalProtocolStore(db_path).read_checkpoint(_context())


@pytest.mark.asyncio
async def test_ac_b1b_049_post_terminal_auxiliary_preserves_root_pointer(db_path: str) -> None:
    done_payload = {
        "candidate_id": "ttc_1",
        "generation": 1,
        "message_key": "terminal.failed",
        "protocol": 1,
        "reason": "execution_failed",
        "receipt_id": "trc_1",
        "required_manifest_template_digest": "manifest_template",
        "selected_variant_key": "primary_none",
        "status": "failed",
    }
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_base(connection)
        _insert_v1_open(connection)
        connection.execute(
            """
            INSERT INTO turn_terminal_candidates(
              candidate_id,turn_id,generation,candidate_origin,reserved_receipt_id,
              reserved_terminal_event_id,finalization_epoch_at_prepare,primary_status,
              primary_reason_code,primary_message_key,closing_accepted_through,
              persisted_event_highwater,terminal_event_counts_by_variant_json,
              candidate_projection_digest,canonicalization_version,state,
              session_disposition_plan_json,session_disposition_plan_digest,
              projection_variant_digests_json,projection_extensions_json,
              created_at,prepared_at,committed_at
            ) VALUES (
              'ttc_1','t_1',1,'normal','trc_1','evt_done',1,'failed',
              'execution_failed','terminal.failed',0,0,'{}','dc_1',1,'committed',
              '{}','plan_1','{}','{}','t','t','t'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO turn_terminal_candidate_variants(
              candidate_id,variant_key,outcome_variant,session_variant,status,reason_code,
              message_key,final_answer_mode,required_manifest_template_digest,
              session_mutation_template_digest,variant_digest
            ) VALUES (
              'ttc_1','primary_none','primary','none','failed','execution_failed',
              'terminal.failed','none','manifest_template','session_template','variant_digest'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO turn_terminal_receipts(
              receipt_id,turn_id,generation,candidate_id,protocol_version,status,reason_code,
              message_key,final_answer_mode,selected_variant_key,selected_outcome_variant,
              selected_session_variant,terminal_event_id,first_event_seq,last_event_seq,
              persisted_event_highwater,required_event_manifest_json,
              candidate_projection_digest,selected_projection_digest,
              selected_manifest_digest,selected_session_mutation_digest,
              projection_extensions_json,committed_at
            ) VALUES (
              'trc_1','t_1',1,'ttc_1',1,'failed','execution_failed','terminal.failed',
              'none','primary_none','primary','none','evt_done',1,1,0,'[]',
              'dc_1','dr_1','manifest_1','mutation_1','{}','t'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO turn_events(
              id,turn_id,seq,event_type,payload_json,occurred_at,protocol_version,
              generation,event_seq,actor_scope,phase_scope,payload_digest,
              terminal_receipt_id
            ) VALUES (
              'evt_done','t_1',1,'done',?,'t',1,1,1,'root','in_generation',?,'trc_1'
            )
            """,
            (canonical_json_v1(done_payload), canonical_digest_v1(done_payload)),
        )
        connection.execute(
            """
            UPDATE turn_generations
            SET state='closed',closing_accepted_through=0,terminal_candidate_id='ttc_1',
                terminal_receipt_id='trc_1',closed_at='t'
            WHERE turn_id='t_1' AND generation=1
            """
        )
        connection.execute(
            """
            UPDATE turns
            SET status='failed',last_terminal_generation=1,
                current_terminal_receipt_id='trc_1',event_sequence_next=2
            WHERE id='t_1'
            """
        )
        connection.commit()
        root_before = connection.execute(
            """
            SELECT status,current_generation,generation_transition_epoch,
                   last_terminal_generation,current_terminal_receipt_id
            FROM turns WHERE id='t_1'
            """
        ).fetchone()

    store = _s1_store(db_path)
    resolve_context = AuthFenceResolveContext(
        team_id="team_1",
        viewer="u_1",
        resource_ref="t_1",
        session_id="s_1",
        required_scopes=(
            AuthFenceScope(AuthFenceScopeKind.USER_ACCOUNT, "__global__", "principal_u1"),
            AuthFenceScope(AuthFenceScopeKind.SESSION_VISIBILITY, "team_1", "session_s1"),
            AuthFenceScope(AuthFenceScopeKind.RESOURCE_GRANT, "team_1", "t_1"),
            AuthFenceScope(AuthFenceScopeKind.TEAM_MEMBERSHIP, "team_1", "member_u1"),
        ),
    )
    auth_fence_vector = await store.ensure_auth_scope_epochs(resolve_context)
    auxiliary_context = PostTerminalAuxiliaryContext(
        scope=TerminalScope("team_1", "s_1", "t_1", 1),
        receipt_id="trc_1",
        event_id="evt_aux_1",
        expected_delivery_fence_epoch=0,
        auth_fence_vector_digest=auth_fence_vector.auth_fence_vector_digest,
    )
    first = await store.append_post_terminal_auxiliary(
        auxiliary_context,
        resolve_context=resolve_context,
        expected_auth_fence_vector=auth_fence_vector,
        event_type="satisfaction.recorded",
        actor_scope=ActorScope.TRANSPORT,
        payload={"rating": 5},
    )
    replay = await store.append_post_terminal_auxiliary(
        auxiliary_context,
        resolve_context=resolve_context,
        expected_auth_fence_vector=auth_fence_vector,
        event_type="satisfaction.recorded",
        actor_scope=ActorScope.TRANSPORT,
        payload={"rating": 5},
    )
    assert replay == first
    assert first.event_seq == 2
    assert first.phase_scope == PhaseScope.POST_TERMINAL_AUXILIARY
    replay_rows = await store.list_protocol_events(_context())
    assert [row.event_id for row in replay_rows] == ["evt_done", "evt_aux_1"]
    assert replay_rows[-1].phase_scope == PhaseScope.POST_TERMINAL_AUXILIARY
    with pytest.raises(TerminalProtocolMalformedError, match="identity conflict"):
        await store.append_post_terminal_auxiliary(
            auxiliary_context,
            resolve_context=resolve_context,
            expected_auth_fence_vector=auth_fence_vector,
            event_type="satisfaction.recorded",
            actor_scope=ActorScope.TRANSPORT,
            payload={"rating": 1},
        )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE terminal_auth_scope_epochs
            SET epoch=epoch+1,row_version=row_version+1,updated_at='revoked'
            WHERE scope_kind='resource_grant' AND team_id='team_1' AND scope_ref='t_1'
            """
        )
        connection.commit()
    with pytest.raises(TerminalProtocolMalformedError, match="auth fence changed"):
        await store.append_post_terminal_auxiliary(
            replace(auxiliary_context, event_id="evt_aux_2"),
            resolve_context=resolve_context,
            expected_auth_fence_vector=auth_fence_vector,
            event_type="satisfaction.recorded",
            actor_scope=ActorScope.TRANSPORT,
            payload={"rating": 5},
        )

    with sqlite3.connect(db_path) as connection:
        root_after = connection.execute(
            """
            SELECT status,current_generation,generation_transition_epoch,
                   last_terminal_generation,current_terminal_receipt_id
            FROM turns WHERE id='t_1'
            """
        ).fetchone()
        assert root_after == root_before
        assert connection.execute(
            "SELECT COUNT(*),MAX(event_seq) FROM turn_events WHERE turn_id='t_1'",
        ).fetchone() == (2, 2)


@pytest.mark.asyncio
async def test_terminal_protocol_reader_lists_only_contiguous_v1_events(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_base(connection)
        _insert_v1_open(connection)
        connection.execute(
            """
            INSERT INTO turn_event_proposals(
              proposal_id,event_id,turn_id,generation,accept_ordinal,accepted_claim_epoch,
              event_type,actor_scope,phase_scope,payload_json,payload_digest,durability,
              state,accepted_at,persisted_at,persisted_event_seq
            ) VALUES ('tep_1','evt_1','t_1',1,1,1,'step_started','step','in_generation',
                      '{}',?,'committed','persisted','t','t',1)
            """,
            (canonical_digest_v1({}),),
        )
        connection.execute(
            """
            INSERT INTO turn_events(
              id,turn_id,seq,event_type,payload_json,occurred_at,protocol_version,generation,
              event_seq,actor_scope,phase_scope,payload_digest,source_proposal_id,accept_ordinal
            ) VALUES ('evt_1','t_1',1,'step_started','{}','t',1,1,1,'step','in_generation',?,'tep_1',1)
            """,
            (canonical_digest_v1({}),),
        )
        connection.execute("UPDATE turns SET event_sequence_next=2 WHERE id='t_1'")
        connection.commit()

    rows = await TerminalProtocolStore(db_path).list_protocol_events(_context())
    assert [row.event_id for row in rows] == ["evt_1"]
    assert rows[0].as_terminal_event().actor_scope == "step"
    assert rows[0].payload_digest == canonical_digest_v1({})


@pytest.mark.asyncio
async def test_terminal_protocol_reader_rejects_event_payload_digest_mismatch(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_base(connection)
        _insert_v1_open(connection)
        connection.execute(
            """
            INSERT INTO turn_event_proposals(
              proposal_id,event_id,turn_id,generation,accept_ordinal,accepted_claim_epoch,
              event_type,actor_scope,phase_scope,payload_json,payload_digest,durability,
              state,accepted_at,persisted_at,persisted_event_seq
            ) VALUES ('tep_1','evt_1','t_1',1,1,1,'step_started','step','in_generation',
                      '{}','wrong','committed','persisted','t','t',1)
            """,
        )
        connection.execute(
            """
            INSERT INTO turn_events(
              id,turn_id,seq,event_type,payload_json,occurred_at,protocol_version,generation,
              event_seq,actor_scope,phase_scope,payload_digest,source_proposal_id,accept_ordinal
            ) VALUES ('evt_1','t_1',1,'step_started','{}','t',1,1,1,'step','in_generation','wrong','tep_1',1)
            """,
        )
        connection.execute("UPDATE turns SET event_sequence_next=2 WHERE id='t_1'")
        connection.commit()

    with pytest.raises(TerminalProtocolMalformedError, match="payload_digest"):
        await TerminalProtocolStore(db_path).list_protocol_events(_context())


@pytest.mark.unit
def test_terminal_protocol_store_exposes_only_approved_writers() -> None:
    public_methods = {name for name in dir(TerminalProtocolStore) if not name.startswith("_") and callable(getattr(TerminalProtocolStore, name))}
    assert public_methods == {
        "read_context_maintenance_action",
        "read_recovery_context_completions",
        "read_recovery_operation_kind",
        "admit_or_steer_v1",
        "authorize_active_v1_control",
        "acquire_recovery_writer_fence",
        "acquire_terminal_writer_lease",
        "aggregate_effect_retention",
        "accept_event",
        "append_post_terminal_auxiliary",
        "begin_cancelled_finalization_with_local_billing",
        "begin_finalization",
        "begin_effect_attempt",
        "claim_generation",
        "claim_effect_dispatch",
        "claim_evidence_purge",
        "claim_recovery",
        "claim_decision_effects",
        "defer_recovery",
        "defer_discovered_recovery",
        "defer_decision_effect_group",
        "defer_pending_decision",
        "discard_effect_intent",
        "commit_candidate",
        "commit_control_generation",
        "commit_evidence_purge",
        "commit_protocol_delete",
        "commit_recovery_candidate",
        "confirm_commit",
        "discover_recovery",
        "drain_terminal_writer_lease",
        "flush_finalization_through",
        "flush_recovery_through",
        "flush_through",
        "freeze_usage_and_claim_billing_attempt",
        "has_effect_retention_key",
        "initialize",
        "ensure_auth_scope_epochs",
        "linearize_frame_enqueue",
        "linearize_subscription_promotion",
        "list_protocol_events",
        "list_recovery_candidates",
        "load_candidate_for_recovery",
        "mark_execution_started",
        "mark_effect_dispatched",
        "mark_effect_unknown",
        "mark_effect_unknown_and_discover_recovery",
        "open_resume_generation",
        "prepare_candidate",
        "prepare_recovery_candidate",
        "purge_expired_effect_retention",
        "read_auth_fence_vector",
        "read_checkpoint",
        "read_committed_followup_template",
        "read_effect_disclosure_snapshot",
        "read_current_effect_disclosure",
        "read_effect_attempt",
        "read_effect_attempt_details",
        "read_effect_execution_contract",
        "read_execution_accepted_highwater",
        "read_frozen_steer_snapshot",
        "read_recovery_frozen_steer_snapshot",
        "read_recovery_effect_context",
        "read_recovery_auth_fence_digest",
        "count_recovery_inventory",
        "discard_recovery_effect_intents",
        "read_generation_pointers",
        "read_web_chat_action",
        "read_persisted_turn_protocol_version",
        "read_persisted_turn_protocol_binding",
        "read_persisted_turn_presentation_locale",
        "read_persisted_turn_scope",
        "list_pending_decisions",
        "list_pending_decision_effect_ids",
        "read_current_decision_auth_digest",
        "read_decision_preflight",
        "read_protocol_summary",
        "read_recovery_next_due_at",
        "read_source_decision",
        "record_admin_probed_effect_settlement",
        "record_approval_projection_effect_settlement",
        "record_effect_settlement",
        "record_manual_effect_settlement",
        "record_provider_effect_settlement",
        "record_decision",
        "settle_decision_effect",
        "skip_decision_effects",
        "record_execution_usage",
        "record_llm_effect_settlement_with_usage",
        "record_tool_effect_settlement",
        "record_local_billing_settlement",
        "heartbeat_execution",
        "handoff_execution_to_recovery",
        "heartbeat_recovery_writer_fence",
        "heartbeat_terminal_writer_lease",
        "read_current_scope",
        "read_decision_control_outcome",
        "read_recovery_discovery_for_scope",
        "read_recovery_max_due_at",
        "read_session_protocol_facts",
        "rehydrate_decision_effect_source",
        "release_recovery_writer_fence",
        "release_terminal_writer_lease",
        "record_unknown_effect_process_event",
        "supersede_pending_decision_with_revoke",
        "verify_execution_usage",
    }


@pytest.mark.asyncio
async def test_active_v1_control_authorization_binds_owner_auth_and_active_tuple(
    db_path: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()
    store = _s1_store(db_path)
    request = _admission_request()
    admitted = await store.admit_or_steer_v1(request)
    assert admitted.disposition is AdmissionDisposition.ADMITTED

    authorized = await store.authorize_active_v1_control(
        context=request.context,
        expected_turn_id="t_1",
    )
    assert authorized.scope == TerminalScope(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        generation=1,
    )
    assert authorized.code is None

    wrong_actor = await store.authorize_active_v1_control(
        context=replace(request.context, actor="u_2"),
        expected_turn_id="t_1",
    )
    assert wrong_actor.scope is None
    assert wrong_actor.code is AdmissionCode.SESSION_ACCESS_DENIED

    wrong_turn = await store.authorize_active_v1_control(
        context=request.context,
        expected_turn_id="t_other",
    )
    assert wrong_turn.scope is None
    assert wrong_turn.code is AdmissionCode.SESSION_TURN_ACTIVE

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE terminal_auth_scope_epochs SET epoch=epoch+1,row_version=row_version+1
            WHERE scope_kind='team_membership' AND team_id='team_1'
            """,
        )
        connection.commit()
    revoked = await store.authorize_active_v1_control(
        context=request.context,
        expected_turn_id="t_1",
    )
    assert revoked.scope is None
    assert revoked.code is AdmissionCode.AUTH_FENCE_CHANGED

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turns").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM turn_control_decisions").fetchone() == (0,)


@pytest.mark.asyncio
async def test_s1_admission_claim_and_execution_started_are_three_durable_fences(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()

    store = _s1_store(db_path)
    admitted = await store.admit_or_steer_v1(_admission_request())
    assert admitted.disposition is AdmissionDisposition.ADMITTED
    assert admitted.scope == TerminalScope(team_id="team_1", session_id="s_1", turn_id="t_1", generation=1)
    with sqlite3.connect(db_path) as connection:
        turn = connection.execute(
            """
            SELECT terminal_protocol_version,terminal_ingress_surface,terminal_admission_cohort,
                   current_generation,generation_transition_epoch,event_sequence_next,
                   delivery_fence_epoch,presentation_locale,message_catalog_version
            FROM turns WHERE id='t_1'
            """,
        ).fetchone()
        generation = connection.execute(
            "SELECT state,row_version,claim_epoch,execution_started_at FROM turn_generations WHERE turn_id='t_1'",
        ).fetchone()
        session = connection.execute(
            """
            SELECT active_turn_id,active_turn_protocol_version,active_turn_generation,
                   active_turn_claim_epoch FROM sessions WHERE id='s_1'
            """,
        ).fetchone()
    assert turn == (1, "web", "bucket=1;config=rollout_1", 1, 1, 1, 0, "zh-CN", 1)
    assert generation == ("opened", 1, 0, None)
    assert session == ("t_1", 1, 1, 0)

    claimed = await store.claim_generation(_claim_context())
    assert claimed.disposition is GenerationClaimDisposition.CLAIMED
    assert claimed.claimed is True
    assert claimed.context is not None
    started = await store.mark_execution_started(claimed.context)
    assert started.generation_row_version == 3
    replayed = await store.mark_execution_started(claimed.context)
    assert replayed == started
    with sqlite3.connect(db_path) as connection:
        generation = connection.execute(
            """
            SELECT state,row_version,owner_instance_id,claim_epoch,writer_lease_epoch_at_claim,
                   execution_started_at FROM turn_generations WHERE turn_id='t_1'
            """,
        ).fetchone()
        session_epoch = connection.execute(
            "SELECT active_turn_claim_epoch FROM sessions WHERE id='s_1'",
        ).fetchone()[0]
    assert generation[:5] == ("running", 3, "gateway_1", 1, 7)
    assert generation[5] is not None
    assert session_epoch == 1


@pytest.mark.asyncio
async def test_s1_admission_preserves_monotonic_session_activity_time(db_path: str) -> None:
    future_activity = "2026-08-04T00:00:00Z"
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.execute("UPDATE sessions SET last_active_at=? WHERE id='s_1'", (future_activity,))
        connection.commit()

    await _s1_store(db_path).admit_or_steer_v1(_admission_request())
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT last_active_at FROM sessions WHERE id='s_1'",
        ).fetchone() == (future_activity,)


@pytest.mark.asyncio
async def test_ac_b1b_021_admission_rollback_has_zero_external_effects(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()

    store = _s1_store(db_path, fault=TerminalProtocolFaultPoint.ADMISSION_BEFORE_COMMIT)
    with pytest.raises(RuntimeError, match="ADMISSION_BEFORE_COMMIT"):
        await store.admit_or_steer_v1(_admission_request())

    assert _protocol_counts(db_path) == {
        "turns": 0,
        "turn_generations": 0,
        "turn_events": 0,
        "turn_effect_attempts": 0,
        "turn_terminal_receipts": 0,
    }
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT active_turn_id,steer_buffer_revision FROM sessions WHERE id='s_1'",
        ).fetchone() == (None, 0)


@pytest.mark.asyncio
async def test_ac_b1b_022_kill_before_claim_converges_opened_generation(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()

    store = _s1_store(db_path, fault=TerminalProtocolFaultPoint.ADMISSION_AFTER_COMMIT)
    with pytest.raises(RuntimeError, match="ADMISSION_AFTER_COMMIT"):
        await store.admit_or_steer_v1(_admission_request())

    assert _protocol_counts(db_path) == {
        "turns": 1,
        "turn_generations": 1,
        "turn_events": 0,
        "turn_effect_attempts": 0,
        "turn_terminal_receipts": 0,
    }
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state,claim_epoch,execution_started_at FROM turn_generations WHERE turn_id='t_1'",
        ).fetchone() == ("opened", 0, None)
        assert connection.execute(
            "SELECT active_turn_id,active_turn_claim_epoch FROM sessions WHERE id='s_1'",
        ).fetchone() == ("t_1", 0)


@pytest.mark.asyncio
async def test_ac_b1b_057_pre_admission_denial_has_zero_protocol_writes(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection, expires_at=_NOW - timedelta(seconds=1))
        connection.commit()

    denied = await _s1_store(db_path).admit_or_steer_v1(_admission_request())
    assert denied.disposition is AdmissionDisposition.REJECTED
    assert denied.code is AdmissionCode.WRITER_NOT_READY
    assert _protocol_counts(db_path) == {
        "turns": 0,
        "turn_generations": 0,
        "turn_events": 0,
        "turn_effect_attempts": 0,
        "turn_terminal_receipts": 0,
    }


@pytest.mark.asyncio
async def test_s1_admission_revalidates_actor_and_deleted_session_without_writes(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()

    original = _admission_request()
    wrong_actor = replace(original, context=replace(original.context, actor="u_other"))
    denied = await _s1_store(db_path).admit_or_steer_v1(wrong_actor)
    assert denied.code is AdmissionCode.SESSION_ACCESS_DENIED
    assert _protocol_counts(db_path)["turns"] == 0

    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE sessions SET deleted_at='2026-08-03T04:00:00Z' WHERE id='s_1'")
        connection.commit()
    deleted = await _s1_store(db_path).admit_or_steer_v1(_admission_request())
    assert deleted.code is AdmissionCode.SESSION_DELETED
    assert _protocol_counts(db_path)["turns"] == 0


@pytest.mark.asyncio
async def test_s1_admission_samples_lease_time_after_acquiring_writer_lock(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()

    clock_calls: list[datetime] = []

    def clock() -> datetime:
        clock_calls.append(_NOW)
        return _NOW

    store = TerminalProtocolStore(db_path, _clock=clock)
    async with open_terminal_protocol_connection(db_path) as lock:
        await lock.execute("BEGIN IMMEDIATE")
        pending = asyncio.create_task(store.admit_or_steer_v1(_admission_request()))
        await asyncio.sleep(0.05)
        assert clock_calls == []
        await lock.rollback()
        admitted = await pending
    assert admitted.disposition is AdmissionDisposition.ADMITTED
    assert clock_calls == [_NOW]


@pytest.mark.asyncio
async def test_s1_admission_request_replay_is_stable_and_conflict_fails_closed(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()

    store = _s1_store(db_path)
    first = await store.admit_or_steer_v1(_admission_request())
    replay = await store.admit_or_steer_v1(_admission_request())
    stale_auth = await store.admit_or_steer_v1(
        _admission_request(auth_fence_vector_digest="auth_digest_after_revalidation"),
    )
    conflict = await store.admit_or_steer_v1(
        _admission_request(user_text="changed", execution_intent={"text": "changed"}),
    )
    assert first.disposition is AdmissionDisposition.ADMITTED
    assert replay.disposition is AdmissionDisposition.ADMITTED_REPLAY
    assert replay.scope == first.scope
    assert stale_auth.code is AdmissionCode.AUTH_FENCE_CHANGED
    assert conflict.code is AdmissionCode.REQUEST_CONFLICT
    assert _protocol_counts(db_path)["turns"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault", "persisted_count"),
    [
        (TerminalProtocolFaultPoint.ADMISSION_BEFORE_COMMIT, 0),
        (TerminalProtocolFaultPoint.ADMISSION_AFTER_COMMIT, 1),
    ],
)
async def test_typed_home_admission_commits_session_turn_generation_and_source_together(
    db_path: str,
    fault: TerminalProtocolFaultPoint,
    persisted_count: int,
) -> None:
    session_id = "s_0123456789abcdef0123456789abcdef"
    request = _typed_admission_request(
        session_id=session_id,
        user_text="",
        attachment_ids=("artifact_1",),
        attachment_refs=({"artifact_id": "artifact_1", "name": "证据.csv"},),
        parts=({"text": "", "attachments": [{"artifact_id": "artifact_1"}]},),
        create_session_if_missing=True,
    )
    with sqlite3.connect(db_path) as connection:
        _insert_home_prerequisites(connection, session_id=session_id)
        _insert_writer_lease(connection)
        connection.commit()

    with pytest.raises(RuntimeError, match=fault.value):
        await _s1_store(db_path, fault=fault).admit_or_steer_v1(request)

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone() == (persisted_count,)
        assert connection.execute(
            "SELECT COUNT(*) FROM turns WHERE id=?",
            (request.turn_id,),
        ).fetchone() == (persisted_count,)
        assert connection.execute(
            "SELECT COUNT(*) FROM turn_generations WHERE turn_id=?",
            (request.turn_id,),
        ).fetchone() == (persisted_count,)
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_turn_user_sources WHERE turn_id=?",
            (request.turn_id,),
        ).fetchone() == (persisted_count,)

    if persisted_count == 0:
        return
    store = _s1_store(db_path)
    replay = await store.admit_or_steer_v1(request)
    assert replay.disposition is AdmissionDisposition.ADMITTED_REPLAY
    assert replay.effective_user_text == ""
    found = await store.read_web_chat_action(
        action_id=request.context.request_id,
        action_kind="start",
        actor="u_1",
        team_id="team_1",
        turn_id=request.turn_id,
    )
    assert found is not None
    source = await SessionStore(db_path).get_chat_turn_user_source(
        turn_id=request.turn_id,
        user_id="u_1",
        workspace_id="ws_1",
    )
    assert source == request.user_source


@pytest.mark.asyncio
async def test_typed_start_never_creates_a_claimed_existing_session_or_becomes_steer(
    db_path: str,
) -> None:
    missing_session_id = "s_0123456789abcdef0123456789abcdef"
    with sqlite3.connect(db_path) as connection:
        _insert_home_prerequisites(connection, session_id=missing_session_id)
        _insert_writer_lease(connection)
        connection.commit()
    missing = await _s1_store(db_path).admit_or_steer_v1(
        _typed_admission_request(session_id=missing_session_id),
    )
    assert missing.code is AdmissionCode.SESSION_NOT_FOUND
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sessions WHERE id=?",
            (missing_session_id,),
        ).fetchone() == (0,)

    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id="s_existing")
        connection.commit()
    first = await _s1_store(db_path).admit_or_steer_v1(
        _typed_admission_request(
            request_id="act_start_11111111111111111111111111111111",
            turn_id="t_11111111111111111111111111111111",
            session_id="s_existing",
        )
    )
    second = await _s1_store(db_path).admit_or_steer_v1(
        _typed_admission_request(
            request_id="act_start_22222222222222222222222222222222",
            turn_id="t_22222222222222222222222222222222",
            session_id="s_existing",
        )
    )
    assert first.disposition is AdmissionDisposition.ADMITTED
    assert second.code is AdmissionCode.SESSION_TURN_ACTIVE
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT steer_buffer_revision,steer_buffer_json FROM sessions WHERE id='s_existing'",
        ).fetchone() == (0, "[]")
        assert connection.execute(
            "SELECT COUNT(*) FROM turns WHERE session_id='s_existing'",
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_turn_user_sources WHERE session_id='s_existing'",
        ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_typed_home_turn_collision_rolls_back_new_session(db_path: str) -> None:
    home_session_id = "s_0123456789abcdef0123456789abcdef"
    turn_id = "t_0123456789abcdef0123456789abcdef"
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id="s_other")
        _insert_home_prerequisites(connection, session_id=home_session_id)
        _insert_writer_lease(connection)
        connection.execute(
            """
            INSERT INTO turns(id,session_id,seq,user_text,status,started_at)
            VALUES (?, 's_other', 1, 'legacy', 'completed', 't')
            """,
            (turn_id,),
        )
        connection.commit()

    result = await _s1_store(db_path).admit_or_steer_v1(
        _typed_admission_request(
            session_id=home_session_id,
            turn_id=turn_id,
            create_session_if_missing=True,
        )
    )
    assert result.code is AdmissionCode.REQUEST_CONFLICT
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sessions WHERE id=?",
            (home_session_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_turn_user_sources WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "error_type"),
    [
        (
            "UPDATE turns SET terminal_protocol_version=2 WHERE id='t_0123456789abcdef0123456789abcdef'",
            TerminalProtocolFutureVersionError,
        ),
        (
            "UPDATE turns SET terminal_protocol_version=0 WHERE id='t_0123456789abcdef0123456789abcdef'",
            TerminalProtocolMalformedError,
        ),
        (
            "UPDATE turn_generations SET protocol_version=2 WHERE turn_id='t_0123456789abcdef0123456789abcdef' AND generation=1",
            TerminalProtocolFutureVersionError,
        ),
        (
            "UPDATE turn_generations SET protocol_version=0 WHERE turn_id='t_0123456789abcdef0123456789abcdef' AND generation=1",
            TerminalProtocolMalformedError,
        ),
        (
            "UPDATE turn_generations SET kind='control' WHERE turn_id='t_0123456789abcdef0123456789abcdef' AND generation=1",
            TerminalProtocolMalformedError,
        ),
        (
            "UPDATE turns SET presentation_locale='' WHERE id='t_0123456789abcdef0123456789abcdef'",
            TerminalProtocolMalformedError,
        ),
        (
            "UPDATE turns SET message_catalog_version=0 WHERE id='t_0123456789abcdef0123456789abcdef'",
            TerminalProtocolMalformedError,
        ),
        (
            "DELETE FROM turn_generations WHERE turn_id='t_0123456789abcdef0123456789abcdef' AND generation=1",
            TerminalProtocolMalformedError,
        ),
    ],
)
async def test_typed_start_exact_replay_fails_closed_on_protocol_shape(
    db_path: str,
    mutation: str,
    error_type: type[Exception],
) -> None:
    request = _typed_admission_request()
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id=request.context.session_id)
        _insert_writer_lease(connection)
        connection.commit()
    store = _s1_store(db_path)
    assert (await store.admit_or_steer_v1(request)).accepted

    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TRIGGER IF EXISTS turns_terminal_protocol_immutable")
        connection.execute("DROP TRIGGER IF EXISTS turns_terminal_protocol_shape_update")
        connection.execute("DROP TRIGGER IF EXISTS turn_generation_identity_immutable")
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(mutation)
        connection.commit()

    with pytest.raises(error_type):
        await store.admit_or_steer_v1(request)


@pytest.mark.asyncio
async def test_typed_fresh_start_rejects_locale_context_mismatch_without_writes(
    db_path: str,
) -> None:
    request = _typed_admission_request()
    request = replace(
        request,
        execution_intent={
            **request.execution_intent,
            "language_context": {
                "data_language": "en-US",
                "output_language": "en-US",
                "ui_language": "en-US",
                "week_start_day": "monday",
            },
        },
        presentation_locale="zh-CN",
    )
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id=request.context.session_id)
        _insert_writer_lease(connection)
        connection.commit()

    result = await _s1_store(db_path).admit_or_steer_v1(request)

    assert result.code is AdmissionCode.REQUEST_CONFLICT
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM turns WHERE session_id=?",
            (request.context.session_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_turn_user_sources WHERE session_id=?",
            (request.context.session_id,),
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(("remove", key), id=f"missing-{key}")
        for key in (
            "active_datarc_project",
            "attachment_refs",
            "attachments",
            "language_context",
            "member_uid",
            "parts",
            "planning_mode",
            "preferred_model",
            "preferred_provider",
            "text",
            "workspace_id",
        )
    ]
    + [pytest.param(("add", "unknown"), id="unknown-extra")],
)
def test_typed_execution_context_requires_exact_canonical_key_set(
    mutation: tuple[str, str],
) -> None:
    execution_intent = dict(_typed_admission_request().execution_intent)
    operation, key = mutation
    if operation == "remove":
        execution_intent.pop(key)
    else:
        execution_intent[key] = None

    assert not _typed_execution_context_matches(execution_intent, "zh-CN")


def test_typed_execution_context_carries_a_viewer_zone_only_beside_a_missing_language_snapshot() -> None:
    """i18n disabled ⇒ no language snapshot; the viewer zone is kept beside it as an exact known name."""
    execution_intent = dict(_typed_admission_request().execution_intent)
    assert execution_intent["language_context"] is None

    assert _typed_execution_context_matches({**execution_intent, "viewer_time_zone": "America/Los_Angeles"}, "zh-CN")
    for bad in ("Mars/Base", " America/Los_Angeles", 8, ""):
        assert not _typed_execution_context_matches({**execution_intent, "viewer_time_zone": bad}, "zh-CN")
    with_language = {**execution_intent, "language_context": LanguageContext(ui_language="zh-CN").to_dict()}
    assert _typed_execution_context_matches(with_language, "zh-CN")
    assert not _typed_execution_context_matches({**with_language, "viewer_time_zone": "America/Los_Angeles"}, "zh-CN")


def test_typed_execution_context_accepts_optional_paradigm_v2_bool() -> None:
    execution_intent = dict(_typed_admission_request().execution_intent)

    assert _typed_execution_context_matches(execution_intent, "zh-CN")
    assert _typed_execution_context_matches(
        {**execution_intent, "paradigm_v2": True},
        "zh-CN",
    )
    assert _typed_execution_context_matches(
        {**execution_intent, "paradigm_v2": False},
        "zh-CN",
    )
    assert not _typed_execution_context_matches(
        {**execution_intent, "paradigm_v2": "false"},
        "zh-CN",
    )


def test_typed_execution_context_accepts_optional_plan_execution_identity() -> None:
    execution_intent = dict(_typed_admission_request().execution_intent)

    assert _typed_execution_context_matches(
        {**execution_intent, "plan_execution_id": "plan_1"},
        "zh-CN",
    )
    assert _typed_execution_context_matches({**execution_intent, "plan_execution_id": "plan_1", "expected_plan_revision": 2}, "zh-CN")
    assert not _typed_execution_context_matches({**execution_intent, "expected_plan_revision": 2}, "zh-CN")
    for revision in (None, 0, -1, True, "2", 1.5):
        assert not _typed_execution_context_matches({**execution_intent, "plan_execution_id": "plan_1", "expected_plan_revision": revision}, "zh-CN")
    for malformed in (None, "", " plan_1", "plan_1 ", "p" * 129, True):
        assert not _typed_execution_context_matches(
            {**execution_intent, "plan_execution_id": malformed},
            "zh-CN",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "remove"),
    [
        ("member_uid", None, True),
        ("member_uid", None, False),
        ("active_datarc_project", True, False),
        ("active_datarc_project", "", False),
        ("planning_mode", 1, False),
        ("preferred_model", True, False),
        ("language_context", {"ui_language": "zh-CN"}, False),
    ],
)
async def test_typed_fresh_start_rejects_incomplete_execution_context_without_writes(
    db_path: str,
    field: str,
    value: object,
    remove: bool,
) -> None:
    request = _typed_admission_request(create_session_if_missing=True)
    execution_intent = dict(request.execution_intent)
    if remove:
        execution_intent.pop(field)
    else:
        execution_intent[field] = value
    request = replace(request, execution_intent=execution_intent)
    with sqlite3.connect(db_path) as connection:
        _insert_home_prerequisites(
            connection,
            session_id=request.context.session_id,
        )
        _insert_writer_lease(connection)
        connection.commit()

    result = await _s1_store(db_path).admit_or_steer_v1(request)

    assert result.code is AdmissionCode.REQUEST_CONFLICT
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sessions WHERE id=?",
            (request.context.session_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM turns WHERE id=?",
            (request.turn_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM turn_generations WHERE turn_id=?",
            (request.turn_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_turn_user_sources WHERE turn_id=?",
            (request.turn_id,),
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_typed_start_legacy_turn_id_collision_remains_a_conflict(
    db_path: str,
) -> None:
    request = _typed_admission_request()
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id=request.context.session_id)
        _insert_writer_lease(connection)
        connection.execute(
            """
            INSERT INTO turns(id,session_id,seq,user_text,status,started_at)
            VALUES (?, ?, 1, 'legacy', 'completed', 't')
            """,
            (request.turn_id, request.context.session_id),
        )
        connection.commit()

    result = await _s1_store(db_path).admit_or_steer_v1(request)
    assert result.code is AdmissionCode.REQUEST_CONFLICT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_protocol_version", "generation_protocol_version", "error_type"),
    [
        (2, None, TerminalProtocolFutureVersionError),
        (0, None, TerminalProtocolMalformedError),
        (1, None, TerminalProtocolMalformedError),
        (1, 2, TerminalProtocolFutureVersionError),
        (1, 0, TerminalProtocolMalformedError),
    ],
)
async def test_typed_idle_start_validates_session_protocol_history(
    db_path: str,
    terminal_protocol_version: int,
    generation_protocol_version: int | None,
    error_type: type[Exception],
) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id="s_history")
        _insert_writer_lease(connection)
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            """
            INSERT INTO turns(
              id,session_id,seq,user_text,status,started_at,
              terminal_protocol_version,terminal_ingress_surface,
              terminal_admission_cohort,protocol_admitted_at,current_generation,
              generation_transition_epoch,event_sequence_next,delivery_fence_epoch,
              presentation_locale,message_catalog_version
            ) VALUES (
              't_history','s_history',1,'old','completed','t',?,'web','all','t',
              CASE WHEN ?=1 THEN 1 ELSE NULL END,
              CASE WHEN ?=1 THEN 1 ELSE NULL END,
              CASE WHEN ?=1 THEN 1 ELSE NULL END,
              CASE WHEN ?=1 THEN 0 ELSE NULL END,
              CASE WHEN ?=1 THEN 'zh-CN' ELSE NULL END,
              CASE WHEN ?=1 THEN 1 ELSE NULL END
            )
            """,
            (
                terminal_protocol_version,
                terminal_protocol_version,
                terminal_protocol_version,
                terminal_protocol_version,
                terminal_protocol_version,
                terminal_protocol_version,
                terminal_protocol_version,
            ),
        )
        if generation_protocol_version is not None:
            connection.execute(
                """
                INSERT INTO turn_generations(
                  turn_id,generation,protocol_version,kind,opened_at,state
                ) VALUES ('t_history',1,?,'execution','t','closed')
                """,
                (generation_protocol_version,),
            )
        connection.commit()

    with pytest.raises(error_type):
        await _s1_store(db_path).admit_or_steer_v1(
            _typed_admission_request(
                request_id="act_start_11111111111111111111111111111111",
                turn_id="t_11111111111111111111111111111111",
                session_id="s_history",
            )
        )


@pytest.mark.asyncio
async def test_typed_start_allows_legacy_history_to_move_forward_to_v1(
    db_path: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id="s_history")
        _insert_writer_lease(connection)
        connection.execute(
            """
            INSERT INTO turns(id,session_id,seq,user_text,status,started_at)
            VALUES ('t_legacy','s_history',1,'old','completed','t')
            """,
        )
        connection.commit()

    result = await _s1_store(db_path).admit_or_steer_v1(
        _typed_admission_request(
            request_id="act_start_11111111111111111111111111111111",
            turn_id="t_11111111111111111111111111111111",
            session_id="s_history",
        )
    )
    assert result.disposition is AdmissionDisposition.ADMITTED


@pytest.mark.asyncio
async def test_session_protocol_facts_are_scope_bound_and_distinguish_legacy_active(
    db_path: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id="s_facts")
        connection.execute(
            """
            INSERT INTO turns(id,session_id,seq,user_text,status,started_at)
            VALUES ('t_legacy','s_facts',1,'old','running','t')
            """,
        )
        connection.execute(
            "UPDATE sessions SET active_turn_id='t_legacy' WHERE id='s_facts'",
        )
        connection.commit()

    store = _s1_store(db_path)
    assert await store.read_session_protocol_facts(
        team_id="team_1",
        actor="u_1",
        workspace_id="ws_1",
        session_id="s_facts",
    ) == SessionProtocolFacts(
        has_v1_history=False,
        legacy_active_turn_id="t_legacy",
    )
    assert (
        await store.read_session_protocol_facts(
            team_id="team_other",
            actor="u_1",
            workspace_id="ws_1",
            session_id="s_facts",
        )
        is None
    )


@pytest.mark.asyncio
async def test_session_protocol_facts_keep_v1_session_sticky_when_idle(
    db_path: str,
) -> None:
    request = _typed_admission_request(session_id="s_v1_facts")
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id=request.context.session_id)
        _insert_writer_lease(connection)
        connection.commit()
    store = _s1_store(db_path)
    admitted = await store.admit_or_steer_v1(request)
    assert admitted.disposition is AdmissionDisposition.ADMITTED

    active = await store.read_session_protocol_facts(
        team_id="team_1",
        actor="u_1",
        workspace_id="ws_1",
        session_id=request.context.session_id,
    )
    assert active is not None
    assert active.has_v1_history is True
    assert active.legacy_active_turn_id is None
    assert active.v1_active_claim is not None
    assert active.v1_active_claim.turn_id == request.turn_id
    assert active.v1_active_claim.generation == 1
    assert active.v1_active_claim.claim_epoch == 0

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE sessions SET active_turn_id=NULL,
              active_turn_protocol_version=NULL,active_turn_generation=NULL,
              active_turn_claim_epoch=NULL
            WHERE id=?
            """,
            (request.context.session_id,),
        )
        connection.commit()

    assert await store.read_session_protocol_facts(
        team_id="team_1",
        actor="u_1",
        workspace_id="ws_1",
        session_id=request.context.session_id,
    ) == SessionProtocolFacts(has_v1_history=True)


@pytest.mark.asyncio
async def test_session_protocol_facts_reject_v1_to_legacy_history(
    db_path: str,
) -> None:
    request = _typed_admission_request(session_id="s_backward")
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id=request.context.session_id)
        _insert_writer_lease(connection)
        connection.commit()
    store = _s1_store(db_path)
    assert (await store.admit_or_steer_v1(request)).accepted
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO turns(id,session_id,seq,user_text,status,started_at)
            VALUES ('t_legacy_after_v1',?,2,'old','completed','t')
            """,
            (request.context.session_id,),
        )
        connection.commit()

    with pytest.raises(
        TerminalProtocolMalformedError,
        match="moved backward",
    ):
        await store.read_session_protocol_facts(
            team_id="team_1",
            actor="u_1",
            workspace_id="ws_1",
            session_id=request.context.session_id,
        )


@pytest.mark.asyncio
async def test_exact_v1_steer_revalidates_full_session_protocol_history(
    db_path: str,
) -> None:
    start = _typed_admission_request(session_id="s_steer_history")
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id=start.context.session_id)
        _insert_writer_lease(connection)
        connection.commit()
    store = _s1_store(db_path)
    assert (await store.admit_or_steer_v1(start)).accepted
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO turns(id,session_id,seq,user_text,status,started_at)
            VALUES ('t_legacy_after_v1',?,2,'old','completed','t')
            """,
            (start.context.session_id,),
        )
        connection.commit()

    with pytest.raises(TerminalProtocolMalformedError, match="moved backward"):
        await store.admit_or_steer_v1(
            _admission_request(
                request_id="act_steer_11111111111111111111111111111111",
                turn_id=start.turn_id,
                session_id=start.context.session_id,
                user_text="next",
                execution_intent={
                    "attachments": [],
                    "planning_mode": None,
                    "text": "next",
                    "workspace_id": "ws_1",
                },
                user_message_id="u_r_11111111111111111111111111111111",
                expected_execution_generation=1,
            )
        )


@pytest.mark.asyncio
async def test_session_protocol_facts_reject_active_generation_claim_mismatch(
    db_path: str,
) -> None:
    request = _typed_admission_request(session_id="s_claim_mismatch")
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id=request.context.session_id)
        _insert_writer_lease(connection)
        connection.commit()
    store = _s1_store(db_path)
    assert (await store.admit_or_steer_v1(request)).accepted
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turn_generations SET claim_epoch=1 WHERE turn_id=? AND generation=1",
            (request.turn_id,),
        )
        connection.commit()

    with pytest.raises(TerminalProtocolMalformedError, match="claim epoch"):
        await store.read_session_protocol_facts(
            team_id="team_1",
            actor="u_1",
            workspace_id="ws_1",
            session_id=request.context.session_id,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corrupt_active_sql",
    [
        """
        UPDATE sessions SET active_turn_id=NULL,
          active_turn_protocol_version=1,active_turn_generation=1,
          active_turn_claim_epoch=0 WHERE id='s_0123456789abcdef0123456789abcdef'
        """,
        """
        UPDATE sessions SET active_turn_protocol_version=NULL,
          active_turn_generation=NULL,active_turn_claim_epoch=NULL
          WHERE id='s_0123456789abcdef0123456789abcdef'
        """,
        """
        UPDATE sessions SET active_turn_id=''
          WHERE id='s_0123456789abcdef0123456789abcdef'
        """,
    ],
)
async def test_typed_start_rejects_malformed_active_session_projection(
    db_path: str,
    corrupt_active_sql: str,
) -> None:
    first = _typed_admission_request()
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id=first.context.session_id)
        _insert_writer_lease(connection)
        connection.commit()
    store = _s1_store(db_path)
    assert (await store.admit_or_steer_v1(first)).accepted
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TRIGGER IF EXISTS sessions_terminal_active_shape_update")
        connection.execute("DROP TRIGGER IF EXISTS sessions_terminal_active_reference_update")
        connection.execute("DROP TRIGGER IF EXISTS sessions_terminal_active_epoch_monotonic")
        connection.execute(corrupt_active_sql)
        connection.commit()

    with pytest.raises(TerminalProtocolMalformedError):
        await store.admit_or_steer_v1(first)


@pytest.mark.asyncio
async def test_typed_start_exact_replay_validates_all_session_history(
    db_path: str,
) -> None:
    first = _typed_admission_request()
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id=first.context.session_id)
        _insert_writer_lease(connection)
        connection.execute(
            """
            INSERT INTO turns(id,session_id,seq,user_text,status,started_at)
            VALUES ('t_legacy_history',?,1,'old','completed','t')
            """,
            (first.context.session_id,),
        )
        connection.commit()
    store = _s1_store(db_path)
    assert (await store.admit_or_steer_v1(first)).accepted

    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TRIGGER IF EXISTS turns_terminal_protocol_immutable")
        connection.execute("DROP TRIGGER IF EXISTS turns_terminal_protocol_no_legacy_upgrade")
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("UPDATE turns SET terminal_protocol_version=2 WHERE id='t_legacy_history'")
        connection.commit()

    with pytest.raises(TerminalProtocolFutureVersionError):
        await store.admit_or_steer_v1(first)


def test_typed_source_envelope_uses_strict_json_types() -> None:
    source = ChatTurnUserSource(
        turn_id="t_1",
        session_id="s_1",
        user_id="u_1",
        workspace_id="ws_1",
        user_text="hello",
        attachment_ids=("artifact_1",),
        attachment_refs=({"artifact_id": "artifact_1", "ordinal": 1},),
        parts=None,
    )
    with pytest.raises(ValueError, match="source envelope changed"):
        _admission_request(
            user_source=source,
            allow_steer=False,
            execution_intent={
                "attachment_refs": [
                    {"artifact_id": "artifact_1", "ordinal": True},
                ],
                "attachments": ["artifact_1"],
                "parts": None,
                "planning_mode": None,
                "text": "hello",
                "workspace_id": "ws_1",
            },
        )


def test_typed_start_cannot_drop_an_input_document_before_reference_protocol_support() -> None:
    from bowhead_core.chat_references import InputDocument

    request = _typed_admission_request()
    document = InputDocument.model_validate({"schema_version": 1, "nodes": [{"type": "text", "text": request.user_text}]})
    source = replace(request.user_source, input_document=document)
    with pytest.raises(ValueError, match="user source envelope changed"):
        replace(request, user_source=source)


@pytest.mark.asyncio
async def test_typed_source_is_snapshotted_before_waiting_for_writer_lock(
    db_path: str,
) -> None:
    attachment_ref: dict[str, object] = {
        "artifact_id": "artifact_1",
        "ordinal": 1,
    }
    request = _typed_admission_request(
        attachment_ids=("artifact_1",),
        attachment_refs=(attachment_ref,),
    )
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id=request.context.session_id)
        _insert_writer_lease(connection)
        connection.commit()

    async with open_terminal_protocol_connection(db_path) as blocker:
        await blocker.execute("BEGIN IMMEDIATE")
        pending = asyncio.create_task(
            _s1_store(db_path).admit_or_steer_v1(request),
        )
        await asyncio.sleep(0.05)
        attachment_ref["ordinal"] = True
        await blocker.rollback()
        result = await pending
    assert result.disposition is AdmissionDisposition.ADMITTED
    source = await SessionStore(db_path).get_chat_turn_user_source(
        turn_id=request.turn_id,
        user_id="u_1",
        workspace_id="ws_1",
    )
    assert source is not None
    assert source.attachment_refs == ({"artifact_id": "artifact_1", "ordinal": 1},)


@pytest.mark.asyncio
async def test_s1_buffered_request_and_reserved_turn_ids_are_global_idempotency_fences(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_base(connection, session_id="s_2")
        _insert_writer_lease(connection)
        connection.commit()

    store = _s1_store(db_path)
    await store.admit_or_steer_v1(_admission_request(request_id="req_active", turn_id="t_active"))
    buffered = await store.admit_or_steer_v1(
        _admission_request(request_id="req_buffered", turn_id="t_reserved"),
    )
    assert buffered.disposition is AdmissionDisposition.STEERED

    reused_request = await store.admit_or_steer_v1(
        _admission_request(request_id="req_buffered", turn_id="t_reserved", session_id="s_2"),
    )
    reused_turn = await store.admit_or_steer_v1(
        _admission_request(request_id="req_other", turn_id="t_reserved", session_id="s_2"),
    )
    assert reused_request.code is AdmissionCode.REQUEST_CONFLICT
    assert reused_turn.code is AdmissionCode.REQUEST_CONFLICT
    assert _protocol_counts(db_path)["turns"] == 1


@pytest.mark.asyncio
async def test_s1_concurrent_v1_admission_has_one_active_winner(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()

    first, second = await asyncio.gather(
        _s1_store(db_path).admit_or_steer_v1(
            _admission_request(request_id="req_web", turn_id="t_web", user_text="web"),
        ),
        _s1_store(db_path).admit_or_steer_v1(
            _admission_request(request_id="req_im", turn_id="t_im", user_text="im"),
        ),
    )
    assert {first.disposition, second.disposition} == {
        AdmissionDisposition.ADMITTED,
        AdmissionDisposition.STEERED,
    }
    winner = first if first.disposition is AdmissionDisposition.ADMITTED else second
    steered = second if winner is first else first
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        session = connection.execute(
            """
            SELECT active_turn_id,active_turn_protocol_version,active_turn_generation,
                   active_turn_claim_epoch,steer_buffer_revision,steer_buffer_json
            FROM sessions WHERE id='s_1'
            """,
        ).fetchone()
        turn_count = connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    assert turn_count == 1
    assert session["active_turn_id"] == winner.active_turn_id
    assert tuple(session[key] for key in ("active_turn_protocol_version", "active_turn_generation", "active_turn_claim_epoch")) == (
        1,
        1,
        0,
    )
    assert session["steer_buffer_revision"] == 1
    buffered = json.loads(session["steer_buffer_json"])
    assert [(item["steer_id"], item["requested_turn_id"], item["revision"]) for item in buffered] == [
        (steered.request_id, "t_im" if steered.request_id == "req_im" else "t_web", 1),
    ]


@pytest.mark.asyncio
async def test_s1_disallowed_or_rolled_back_steer_does_not_mutate_buffer(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()
    store = _s1_store(db_path)
    await store.admit_or_steer_v1(_admission_request())

    disallowed = await store.admit_or_steer_v1(
        _admission_request(request_id="req_2", turn_id="t_2", allow_steer=False),
    )
    assert disallowed.code is AdmissionCode.SESSION_TURN_ACTIVE
    with pytest.raises(RuntimeError, match="ADMISSION_BEFORE_COMMIT"):
        await _s1_store(
            db_path,
            fault=TerminalProtocolFaultPoint.ADMISSION_BEFORE_COMMIT,
        ).admit_or_steer_v1(_admission_request(request_id="req_3", turn_id="t_3"))
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT steer_buffer_revision,steer_buffer_json FROM sessions WHERE id='s_1'",
        ).fetchone() == (0, "[]")


@pytest.mark.asyncio
async def test_exact_v1_steer_atomically_accepts_buffer_and_ack_before_execution_start(
    db_path: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()
    store = _s1_store(db_path)
    admitted = await store.admit_or_steer_v1(_admission_request())
    assert admitted.disposition is AdmissionDisposition.ADMITTED
    claimed = await store.claim_generation(_claim_context())
    assert claimed.context is not None

    action_id = "act_steer_0123456789abcdef0123456789abcdef"
    user_message_id = "u_r_0123456789abcdef0123456789abcdef"
    request = _admission_request(
        request_id=action_id,
        turn_id="t_1",
        user_text="also compare north",
        execution_intent={
            "attachments": [],
            "planning_mode": None,
            "text": "also compare north",
            "workspace_id": "ws_1",
        },
        expected_execution_generation=1,
        user_message_id=user_message_id,
    )
    accepted = await store.admit_or_steer_v1(request)
    replay = await store.admit_or_steer_v1(request)
    conflict = await store.admit_or_steer_v1(
        replace(
            request,
            user_message_id="u_r_11111111111111111111111111111111",
        )
    )
    generation_conflict = await store.admit_or_steer_v1(
        replace(
            request,
            expected_execution_generation=2,
        )
    )

    assert accepted.disposition is AdmissionDisposition.STEERED
    assert replay.disposition is AdmissionDisposition.STEER_REPLAY
    assert conflict.code is AdmissionCode.REQUEST_CONFLICT
    assert generation_conflict.code is AdmissionCode.REQUEST_CONFLICT
    assert generation_conflict.request_digest != accepted.request_digest
    assert accepted.steer_ack_proposal is not None
    assert accepted.steer_ack_proposal == replay.steer_ack_proposal
    assert accepted.steer_ack_proposal.accept_ordinal == 1
    assert accepted.execution_started is False
    assert replay.execution_started is False
    with pytest.raises(
        TerminalProtocolMalformedError,
        match="has not started",
    ):
        await store.accept_event(
            claimed.context,
            event_type="step_started",
            actor_scope=ActorScope.STEP,
            phase_scope=PhaseScope.IN_GENERATION,
            payload={"step": 1},
        )
    lookup = await store.read_web_chat_action(
        action_id=action_id,
        action_kind="steer",
        actor="u_1",
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        execution_generation=1,
        user_message_id=user_message_id,
    )
    assert lookup is not None
    assert lookup.request_digest is None
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT steer_buffer_revision FROM sessions WHERE id='s_1'",
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT accepted_proposal_next FROM turn_generations WHERE turn_id='t_1' AND generation=1",
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM turn_event_proposals WHERE event_type='emoji_ack'",
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM turn_events WHERE event_type='emoji_ack'",
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_exact_v1_steer_accepts_recovery_only_draining_writer(
    db_path: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()
    store = _s1_store(db_path)
    await store.admit_or_steer_v1(_admission_request())
    claimed = await store.claim_generation(_claim_context())
    assert claimed.context is not None
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE terminal_protocol_writer_lease
            SET admission_state='draining',admission_epoch=4
            WHERE lease_name='v1_writer'
            """,
        )
        connection.commit()

    accepted = await store.admit_or_steer_v1(
        _admission_request(
            request_id="act_steer_dddddddddddddddddddddddddddddddd",
            turn_id="t_1",
            user_text="continue during recovery",
            execution_intent={
                "attachments": [],
                "planning_mode": None,
                "text": "continue during recovery",
                "workspace_id": "ws_1",
            },
            expected_execution_generation=1,
            user_message_id="u_r_dddddddddddddddddddddddddddddddd",
            writer_admission_epoch=4,
        )
    )

    assert accepted.disposition is AdmissionDisposition.STEERED
    assert accepted.steer_ack_proposal is not None
    assert accepted.execution_started is False


@pytest.mark.asyncio
async def test_sticky_v1_start_rechecks_history_under_recovery_writer(
    db_path: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_base(connection, session_id="s_2")
        _insert_writer_lease(connection)
        connection.commit()
    store = _s1_store(db_path)
    first = await store.admit_or_steer_v1(
        _typed_admission_request(
            request_id="req_1",
            turn_id="t_1",
            session_id="s_1",
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

    sticky = await store.admit_or_steer_v1(
        _typed_admission_request(
            request_id="req_2",
            turn_id="t_2",
            session_id="s_1",
            writer_admission_epoch=4,
            require_existing_v1_history=True,
        )
    )
    never_v1 = await store.admit_or_steer_v1(
        _typed_admission_request(
            request_id="req_3",
            turn_id="t_3",
            session_id="s_2",
            writer_admission_epoch=4,
            require_existing_v1_history=True,
        )
    )

    assert sticky.disposition is AdmissionDisposition.ADMITTED
    assert never_v1.code is AdmissionCode.REQUEST_CONFLICT


@pytest.mark.asyncio
async def test_exact_v1_steer_rolls_back_buffer_and_ack_together(
    db_path: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()
    store = _s1_store(db_path)
    await store.admit_or_steer_v1(_admission_request())
    claimed = await store.claim_generation(_claim_context())
    assert claimed.context is not None
    request = _admission_request(
        request_id="act_steer_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        turn_id="t_1",
        user_text="next",
        execution_intent={
            "attachments": [],
            "planning_mode": None,
            "text": "next",
            "workspace_id": "ws_1",
        },
        expected_execution_generation=1,
        user_message_id="u_r_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )

    with pytest.raises(RuntimeError, match="ADMISSION_BEFORE_COMMIT"):
        await _s1_store(
            db_path,
            fault=TerminalProtocolFaultPoint.ADMISSION_BEFORE_COMMIT,
        ).admit_or_steer_v1(request)

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT steer_buffer_revision,steer_buffer_json FROM sessions WHERE id='s_1'",
        ).fetchone() == (0, "[]")
        assert connection.execute(
            "SELECT accepted_proposal_next FROM turn_generations WHERE turn_id='t_1' AND generation=1",
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM turn_event_proposals",
        ).fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "error_match", "expected_code"),
    (
        (
            "UPDATE terminal_protocol_writer_lease SET owner_instance_id='writer_other' WHERE lease_name='v1_writer'",
            None,
            AdmissionCode.WRITER_NOT_READY,
        ),
        (
            "UPDATE turn_generations SET lease_expires_at='2020-01-01T00:00:00Z' WHERE turn_id='t_1' AND generation=1",
            "ownership fence changed",
            None,
        ),
        (
            "UPDATE turn_generations SET state='closing',closing_accepted_through=0 WHERE turn_id='t_1' AND generation=1",
            "ownership fence changed",
            None,
        ),
    ),
)
async def test_exact_v1_steer_rechecks_generation_fences_before_any_write(
    db_path: str,
    mutation: str,
    error_match: str | None,
    expected_code: AdmissionCode | None,
) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()
    store = _s1_store(db_path)
    await store.admit_or_steer_v1(_admission_request())
    claimed = await store.claim_generation(_claim_context())
    assert claimed.context is not None
    with sqlite3.connect(db_path) as connection:
        connection.execute(mutation)
        connection.commit()

    request = _admission_request(
        request_id="act_steer_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        turn_id="t_1",
        user_text="next",
        execution_intent={
            "attachments": [],
            "planning_mode": None,
            "text": "next",
            "workspace_id": "ws_1",
        },
        expected_execution_generation=1,
        user_message_id="u_r_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    )
    if expected_code is not None:
        result = await store.admit_or_steer_v1(request)
        assert result.code is expected_code
    else:
        assert error_match is not None
        with pytest.raises(TerminalProtocolMalformedError, match=error_match):
            await store.admit_or_steer_v1(request)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT steer_buffer_revision,steer_buffer_json FROM sessions WHERE id='s_1'",
        ).fetchone() == (0, "[]")
        assert connection.execute(
            "SELECT COUNT(*) FROM turn_event_proposals",
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_exact_v1_steer_replay_rejects_uncommitted_proposal_allocator(
    db_path: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()
    store = _s1_store(db_path)
    await store.admit_or_steer_v1(_admission_request())
    claimed = await store.claim_generation(_claim_context())
    assert claimed.context is not None
    request = _admission_request(
        request_id="act_steer_ffffffffffffffffffffffffffffffff",
        turn_id="t_1",
        user_text="next",
        execution_intent={
            "attachments": [],
            "planning_mode": None,
            "text": "next",
            "workspace_id": "ws_1",
        },
        expected_execution_generation=1,
        user_message_id="u_r_ffffffffffffffffffffffffffffffff",
    )
    accepted = await store.admit_or_steer_v1(request)
    assert accepted.disposition is AdmissionDisposition.STEERED
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "DROP TRIGGER IF EXISTS terminal_generation_envelope_immutable",
        )
        connection.execute(
            "UPDATE turn_generations SET accepted_proposal_next=1 WHERE turn_id='t_1' AND generation=1",
        )
        connection.commit()

    with pytest.raises(
        TerminalProtocolMalformedError,
        match="allocator was not committed",
    ):
        await store.admit_or_steer_v1(request)


@pytest.mark.asyncio
async def test_exact_v1_steer_rejects_stale_target_without_writes(
    db_path: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()
    store = _s1_store(db_path)
    await store.admit_or_steer_v1(_admission_request())
    claimed = await store.claim_generation(_claim_context())
    assert claimed.context is not None

    stale = await store.admit_or_steer_v1(
        _admission_request(
            request_id="act_steer_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            turn_id="t_stale",
            user_text="next",
            execution_intent={
                "attachments": [],
                "planning_mode": None,
                "text": "next",
                "workspace_id": "ws_1",
            },
            expected_execution_generation=1,
            user_message_id="u_r_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        )
    )
    stale_generation = await store.admit_or_steer_v1(
        _admission_request(
            request_id="act_steer_cccccccccccccccccccccccccccccccc",
            turn_id="t_1",
            user_text="next",
            execution_intent={
                "attachments": [],
                "planning_mode": None,
                "text": "next",
                "workspace_id": "ws_1",
            },
            expected_execution_generation=2,
            user_message_id="u_r_cccccccccccccccccccccccccccccccc",
        )
    )

    assert stale.code is AdmissionCode.STEER_TARGET_CHANGED
    assert stale_generation.code is AdmissionCode.STEER_TARGET_CHANGED
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT steer_buffer_revision,steer_buffer_json FROM sessions WHERE id='s_1'",
        ).fetchone() == (0, "[]")
        assert connection.execute(
            "SELECT accepted_proposal_next FROM turn_generations WHERE turn_id='t_1' AND generation=1",
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM turn_event_proposals",
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_exact_v1_steer_never_promotes_idle_or_deferred_session_to_start(
    db_path: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()
    store = _s1_store(db_path)
    request = _admission_request(
        request_id="act_steer_dddddddddddddddddddddddddddddddd",
        turn_id="t_1",
        user_text="next",
        execution_intent={
            "attachments": [],
            "planning_mode": None,
            "text": "next",
            "workspace_id": "ws_1",
        },
        expected_execution_generation=1,
        user_message_id="u_r_dddddddddddddddddddddddddddddddd",
    )

    idle = await store.admit_or_steer_v1(request)
    assert idle.code is AdmissionCode.STEER_TARGET_CHANGED
    assert _protocol_counts(db_path)["turns"] == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT steer_buffer_revision,steer_buffer_json FROM sessions WHERE id='s_1'",
        ).fetchone() == (0, "[]")
        assert connection.execute(
            "SELECT COUNT(*) FROM turn_event_proposals",
        ).fetchone() == (0,)

    await store.admit_or_steer_v1(_admission_request())
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE sessions SET active_turn_id=NULL,
              active_turn_protocol_version=NULL,active_turn_generation=NULL,
              active_turn_claim_epoch=NULL,steer_buffer_state='deferred'
            WHERE id='s_1'
            """,
        )
        connection.commit()
    deferred = await store.admit_or_steer_v1(request)
    assert deferred.code is AdmissionCode.STEER_TARGET_CHANGED
    assert _protocol_counts(db_path)["turns"] == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT steer_buffer_revision,steer_buffer_json FROM sessions WHERE id='s_1'",
        ).fetchone() == (0, "[]")
        assert connection.execute(
            "SELECT COUNT(*) FROM turn_event_proposals",
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_ac_b1b_060_web_im_concurrent_admission_has_one_active_winner(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()

    v1_result, legacy_result = await asyncio.gather(
        _s1_store(db_path).admit_or_steer_v1(
            _admission_request(request_id="req_web", turn_id="t_web", user_text="web"),
        ),
        SessionStore(db_path).record_turn(turn_id="t_im", session_id="s_1", user_text="im"),
        return_exceptions=True,
    )
    assert not isinstance(v1_result, BaseException)
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        turns = connection.execute(
            "SELECT id,terminal_protocol_version FROM turns ORDER BY id",
        ).fetchall()
        session = connection.execute(
            """
            SELECT active_turn_id,active_turn_protocol_version,active_turn_generation,
                   active_turn_claim_epoch,steer_buffer_revision FROM sessions WHERE id='s_1'
            """,
        ).fetchone()
    assert len(turns) == 1
    if isinstance(legacy_result, BaseException):
        assert isinstance(
            legacy_result,
            (SessionProtocolManagedError, sqlite3.IntegrityError),
        )
        assert v1_result.disposition is AdmissionDisposition.ADMITTED
        assert (turns[0]["id"], turns[0]["terminal_protocol_version"]) == ("t_web", 1)
        assert tuple(session) == ("t_web", 1, 1, 0, 0)
    else:
        assert v1_result.disposition is AdmissionDisposition.STEERED
        assert (turns[0]["id"], turns[0]["terminal_protocol_version"]) == ("t_im", None)
        assert tuple(session) == ("t_im", None, None, None, 1)


@pytest.mark.asyncio
async def test_legacy_continuation_writers_leave_v1_parent_projection_untouched(
    db_path: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id="s_v1_continuation")
        _insert_writer_lease(connection)
        connection.commit()
    terminal_store = _s1_store(db_path)
    request = _admission_request(
        request_id="act_start_cccccccccccccccccccccccccccccccc",
        turn_id="t_v1_continuation",
        session_id="s_v1_continuation",
    )
    assert (await terminal_store.admit_or_steer_v1(request)).accepted
    now = _NOW.isoformat()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE sessions SET steer_buffer_json=? WHERE id=?",
            (
                json.dumps([{"text": "must stay buffered"}]),
                "s_v1_continuation",
            ),
        )
        connection.execute(
            "INSERT INTO chat_turn_spawn_dispatch("
            "turn_id,session_id,source_submission_ids_json,dispatch_kind,state,"
            "lease_expires_at,created_at,updated_at"
            ") VALUES (?,?,'[]','start','drain_pending',?,?,?)",
            ("t_v1_continuation", "s_v1_continuation", now, now, now),
        )
        before_turn = connection.execute(
            "SELECT status,ended_at,error,error_kind,current_generation FROM turns WHERE id='t_v1_continuation'",
        ).fetchone()
        before_session = connection.execute(
            "SELECT active_turn_id,active_turn_protocol_version,"
            "active_turn_generation,active_turn_claim_epoch,steer_buffer_json "
            "FROM sessions WHERE id='s_v1_continuation'",
        ).fetchone()
        connection.commit()

    legacy_store = SessionStore(db_path)
    assert (
        await legacy_store.claim_chat_spawn_dispatch_for_worker(
            turn_id="t_v1_continuation",
            owner_id="legacy-owner",
        )
        == "rejected"
    )
    assert (
        await legacy_store.advance_turn_execution_generation(
            turn_id="t_v1_continuation",
        )
        is None
    )
    assert (
        await legacy_store.get_active_resume_dispatch_generation(
            turn_id="t_v1_continuation",
        )
        is None
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET state='claimed',"
            "lease_kind='resume',lease_execution_generation=1,owner_id=NULL "
            "WHERE turn_id='t_v1_continuation'",
        )
        connection.commit()
    assert not await legacy_store.rollback_turn_resume_generation(
        turn_id="t_v1_continuation",
        expected_execution_generation=1,
        owner_id=None,
    )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state,lease_kind,lease_execution_generation FROM chat_turn_spawn_dispatch WHERE turn_id='t_v1_continuation'",
        ).fetchone() == ("claimed", "resume", 1)
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET state='drain_pending',"
            "lease_kind='turn',lease_execution_generation=1 "
            "WHERE turn_id='t_v1_continuation'",
        )
        connection.commit()
    assert not await legacy_store.mark_chat_spawn_dispatch_drain_pending(
        turn_id="t_v1_continuation",
        session_id="s_v1_continuation",
    )
    with pytest.raises(SessionProtocolManagedError, match="isolated"):
        await legacy_store.materialize_steer_follow_up(
            session_id="s_v1_continuation",
            current_turn_id="t_v1_continuation",
            next_turn_id="t_forbidden_legacy_child",
        )

    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT status,ended_at,error,error_kind,current_generation FROM turns WHERE id='t_v1_continuation'",
            ).fetchone()
            == before_turn
        )
        assert (
            connection.execute(
                "SELECT active_turn_id,active_turn_protocol_version,"
                "active_turn_generation,active_turn_claim_epoch,steer_buffer_json "
                "FROM sessions WHERE id='s_v1_continuation'",
            ).fetchone()
            == before_session
        )
        assert connection.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id='t_v1_continuation'",
        ).fetchone() == ("failed",)
        assert connection.execute(
            "SELECT COUNT(*) FROM turns WHERE id='t_forbidden_legacy_child'",
        ).fetchone() == (0,)
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET state='drain_pending' WHERE turn_id='t_v1_continuation'",
        )
        connection.commit()

    assert await legacy_store.recover_expired_chat_spawn_dispatches() == [
        "t_v1_continuation",
    ]
    assert await legacy_store.recover_expired_chat_spawn_dispatches() == []
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT status,ended_at,error,error_kind,current_generation FROM turns WHERE id='t_v1_continuation'",
            ).fetchone()
            == before_turn
        )
        assert (
            connection.execute(
                "SELECT active_turn_id,active_turn_protocol_version,"
                "active_turn_generation,active_turn_claim_epoch,steer_buffer_json "
                "FROM sessions WHERE id='s_v1_continuation'",
            ).fetchone()
            == before_session
        )
        assert connection.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id='t_v1_continuation'",
        ).fetchone() == ("failed",)
        assert connection.execute(
            "SELECT COUNT(*) FROM turns WHERE session_id='s_v1_continuation'",
        ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_mixed_history_continuation_isolation_never_mutates_legacy_parent(
    db_path: str,
) -> None:
    now = _NOW.isoformat()
    parent_ids: dict[str, str] = {}
    with sqlite3.connect(db_path) as connection:
        for suffix in ("materialize", "recovery"):
            session_id = f"s_mixed_{suffix}"
            parent_turn_id = f"t_mixed_{suffix}_legacy"
            parent_ids[suffix] = parent_turn_id
            _insert_base(connection, session_id=session_id)
            connection.execute(
                "INSERT INTO turns(id,session_id,seq,user_text,status,started_at) VALUES (?, ?, 1, 'legacy parent', 'running', ?)",
                (parent_turn_id, session_id, now),
            )
            connection.execute(
                "UPDATE sessions SET active_turn_id=?,steer_buffer_json=? WHERE id=?",
                (
                    parent_turn_id,
                    json.dumps([{"text": "must stay buffered"}]),
                    session_id,
                ),
            )
            _insert_v1_open(
                connection,
                session_id=session_id,
                turn_id=f"t_mixed_{suffix}_v1",
                seq=2,
            )
            connection.execute(
                "INSERT INTO chat_turn_spawn_dispatch("
                "turn_id,session_id,source_submission_ids_json,dispatch_kind,"
                "state,lease_expires_at,created_at,updated_at"
                ") VALUES (?,?,'[]','start','drain_pending',?,?,?)",
                (parent_turn_id, session_id, now, now, now),
            )
        connection.commit()

    store = SessionStore(db_path)
    with pytest.raises(SessionProtocolManagedError):
        await store.materialize_steer_follow_up(
            session_id="s_mixed_materialize",
            current_turn_id=parent_ids["materialize"],
            next_turn_id="t_mixed_materialize_child",
        )

    assert await store.recover_expired_chat_spawn_dispatches() == [
        parent_ids["recovery"],
    ]
    assert await store.recover_expired_chat_spawn_dispatches() == []

    with sqlite3.connect(db_path) as connection:
        for suffix in ("materialize", "recovery"):
            session_id = f"s_mixed_{suffix}"
            parent_turn_id = parent_ids[suffix]
            assert connection.execute(
                "SELECT status,ended_at,error,error_kind FROM turns WHERE id=?",
                (parent_turn_id,),
            ).fetchone() == ("running", None, None, None)
            assert connection.execute(
                "SELECT active_turn_id,active_turn_protocol_version,steer_buffer_json FROM sessions WHERE id=?",
                (session_id,),
            ).fetchone() == (
                parent_turn_id,
                None,
                json.dumps([{"text": "must stay buffered"}]),
            )
            assert connection.execute(
                "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id=?",
                (parent_turn_id,),
            ).fetchone() == ("failed",)
        assert connection.execute(
            "SELECT COUNT(*) FROM turns WHERE id='t_mixed_materialize_child'",
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_stale_cleanup_never_mutates_mixed_history_parent(
    db_path: str,
) -> None:
    session_id = "s_mixed_cleanup"
    parent_turn_id = "t_mixed_cleanup_legacy"
    buffer_json = json.dumps([{"text": "must stay buffered"}])
    old = (_NOW - timedelta(minutes=20)).isoformat()
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id=session_id)
        connection.execute(
            "INSERT INTO turns(id,session_id,seq,user_text,status,started_at) VALUES (?, ?, 1, 'legacy parent', 'running', ?)",
            (parent_turn_id, session_id, old),
        )
        connection.execute(
            "UPDATE sessions SET active_turn_id=?,steer_buffer_json=? WHERE id=?",
            (parent_turn_id, buffer_json, session_id),
        )
        _insert_v1_open(
            connection,
            session_id=session_id,
            turn_id="t_mixed_cleanup_v1",
            seq=2,
        )
        connection.commit()

    store = SessionStore(db_path)
    assert await store.cleanup_stale_running_turns(older_than_min=10) == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT status,ended_at,error,error_kind FROM turns WHERE id=?",
            (parent_turn_id,),
        ).fetchone() == ("running", None, None, None)
        assert connection.execute(
            "SELECT active_turn_id,active_turn_protocol_version,steer_buffer_json FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone() == (parent_turn_id, None, buffer_json)
        assert connection.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (parent_turn_id,),
        ).fetchone() == ("drain_pending",)

    assert await store.recover_expired_chat_spawn_dispatches() == [
        parent_turn_id,
    ]
    assert await store.recover_expired_chat_spawn_dispatches() == []
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT status,ended_at,error,error_kind FROM turns WHERE id=?",
            (parent_turn_id,),
        ).fetchone() == ("running", None, None, None)
        assert connection.execute(
            "SELECT active_turn_id,active_turn_protocol_version,steer_buffer_json FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone() == (parent_turn_id, None, buffer_json)
        assert connection.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (parent_turn_id,),
        ).fetchone() == ("failed",)


@pytest.mark.asyncio
async def test_s1_frozen_and_deferred_buffers_reject_without_mutation(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()
    store = _s1_store(db_path)
    await store.admit_or_steer_v1(_admission_request())
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE sessions SET steer_buffer_state='frozen',steer_freeze_turn_id='t_1',
              steer_freeze_generation=1,steer_freeze_claim_epoch=0,steer_frozen_revision=0
            WHERE id='s_1'
            """,
        )
        connection.commit()
    frozen = await store.admit_or_steer_v1(_admission_request(request_id="req_2", turn_id="t_2"))
    assert frozen.code is AdmissionCode.SESSION_TRANSITIONING
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE sessions SET active_turn_id=NULL,active_turn_protocol_version=NULL,
              active_turn_generation=NULL,active_turn_claim_epoch=NULL,steer_buffer_state='deferred',
              steer_freeze_turn_id=NULL,steer_freeze_generation=NULL,
              steer_freeze_claim_epoch=NULL,steer_frozen_revision=NULL
            WHERE id='s_1'
            """,
        )
        connection.commit()
    with pytest.raises(TerminalProtocolMalformedError, match="empty steer buffer"):
        await store.admit_or_steer_v1(_admission_request(request_id="req_3", turn_id="t_3"))
    assert _protocol_counts(db_path)["turns"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        TerminalProtocolFaultPoint.CLAIM_AFTER_GENERATION_CAS,
        TerminalProtocolFaultPoint.CLAIM_AFTER_SESSION_EPOCH_CAS,
    ],
)
async def test_s1_claim_partial_cas_faults_rollback_both_epochs(
    db_path: str,
    fault: TerminalProtocolFaultPoint,
) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()
    await _s1_store(db_path).admit_or_steer_v1(_admission_request())

    with pytest.raises(RuntimeError, match=str(fault)):
        await _s1_store(db_path, fault=fault).claim_generation(_claim_context())
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state,row_version,claim_epoch,owner_instance_id FROM turn_generations WHERE turn_id='t_1'",
        ).fetchone() == ("opened", 1, 0, None)
        assert connection.execute(
            "SELECT active_turn_claim_epoch FROM sessions WHERE id='s_1'",
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_s1_claim_requires_current_writer_lease_without_mutating_epochs(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()
    await _s1_store(db_path).admit_or_steer_v1(_admission_request())
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE terminal_protocol_writer_lease SET owner_instance_id='gateway_2',epoch=8",
        )
        connection.commit()

    denied = await _s1_store(db_path).claim_generation(_claim_context())
    assert denied.code is GenerationClaimCode.WRITER_LEASE_UNAVAILABLE
    assert denied.claimed is False
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state,row_version,claim_epoch,owner_instance_id FROM turn_generations WHERE turn_id='t_1'",
        ).fetchone() == ("opened", 1, 0, None)
        assert connection.execute(
            "SELECT active_turn_claim_epoch FROM sessions WHERE id='s_1'",
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_s1_claim_commit_ack_loss_keeps_generation_and_session_epoch_equal(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()
    await _s1_store(db_path).admit_or_steer_v1(_admission_request())

    with pytest.raises(RuntimeError, match="CLAIM_AFTER_COMMIT"):
        await _s1_store(db_path, fault=TerminalProtocolFaultPoint.CLAIM_AFTER_COMMIT).claim_generation(_claim_context())
    replay = await _s1_store(db_path).claim_generation(_claim_context())
    assert replay.disposition is GenerationClaimDisposition.CLAIM_REPLAY
    assert replay.claimed is False
    assert replay.context is None
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state,row_version,claim_epoch FROM turn_generations WHERE turn_id='t_1'",
        ).fetchone() == ("running", 2, 1)
        assert connection.execute(
            "SELECT active_turn_claim_epoch FROM sessions WHERE id='s_1'",
        ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_s1_concurrent_claim_returns_one_task_authority_and_one_attach(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()
    await _s1_store(db_path).admit_or_steer_v1(_admission_request())

    results = await asyncio.gather(
        _s1_store(db_path).claim_generation(_claim_context()),
        _s1_store(db_path).claim_generation(_claim_context()),
    )
    assert [result.disposition for result in results].count(GenerationClaimDisposition.CLAIMED) == 1
    assert [result.disposition for result in results].count(GenerationClaimDisposition.CLAIM_REPLAY) == 1
    assert [result.claimed for result in results] == [result.disposition is GenerationClaimDisposition.CLAIMED for result in results]


@pytest.mark.asyncio
async def test_s1_execution_started_fault_and_owner_loss_leave_no_started_marker(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()
    await _s1_store(db_path).admit_or_steer_v1(_admission_request())
    claimed = await _s1_store(db_path).claim_generation(_claim_context())
    assert claimed.context is not None

    with pytest.raises(RuntimeError, match="EXECUTION_BEFORE_STARTED"):
        await _s1_store(db_path, fault=TerminalProtocolFaultPoint.EXECUTION_BEFORE_STARTED).mark_execution_started(
            claimed.context,
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT row_version,execution_started_at FROM turn_generations WHERE turn_id='t_1'",
        ).fetchone() == (2, None)
        connection.execute(
            "UPDATE turn_generations SET lease_expires_at=? WHERE turn_id='t_1'",
            ((_NOW - timedelta(seconds=1)).isoformat(),),
        )
        connection.commit()
    with pytest.raises(TerminalProtocolMalformedError, match="ownership fence changed"):
        await _s1_store(db_path).mark_execution_started(claimed.context)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT execution_started_at FROM turn_generations WHERE turn_id='t_1'",
        ).fetchone() == (None,)


@pytest.mark.asyncio
async def test_s1_recovery_lock_and_prior_effect_unknown_block_admission(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_base(connection)
        _insert_writer_lease(connection)
        _insert_v1_open(connection, turn_id="t_old")
        connection.execute(
            """
            INSERT INTO turn_recovery_records(
              recovery_id,turn_id,generation,scope,kind,phase,state,
              discovered_generation_row_version,created_at
            ) VALUES ('rec_1','t_old',1,'terminal','opened_owner_lost','claim','active',1,'t')
            """,
        )
        connection.execute(
            "UPDATE turn_generations SET state='recovery_pending',closing_accepted_through=0 WHERE turn_id='t_old'",
        )
        connection.execute(
            """
            UPDATE sessions SET recovery_lock_turn_id='t_old',recovery_lock_generation=1,
              recovery_lock_reason='owner_lost' WHERE id='s_1'
            """,
        )
        connection.commit()
    locked = await _s1_store(db_path).admit_or_steer_v1(_admission_request())
    assert locked.code is AdmissionCode.SESSION_RECOVERY_LOCKED

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE sessions SET recovery_lock_turn_id=NULL,recovery_lock_generation=NULL,recovery_lock_reason=NULL WHERE id='s_1'",
        )
        connection.execute(
            """
            INSERT INTO turn_effect_attempts(
              attempt_id,turn_id,generation,claim_epoch,effect_class,provider,action_ref,
              non_retriable,state,started_at
            ) VALUES ('eff_1','t_old',1,1,'side_effect_possible','provider','action_1',1,'unknown','t')
            """,
        )
        connection.execute(
            """
            INSERT INTO turn_terminal_deletion_ledgers(
              deletion_id,turn_id,generation,prior_generation_state,prior_row_version,
              prior_execution_claim_epoch,prior_finalization_epoch,candidate_id,receipt_id,
              old_session_active_tuple,old_session_recovery_tuple,new_delivery_fence_epoch,
              unsettled_effect_count,effect_identity_digest,effect_bundle_digest,
              effect_audit_key_version,effect_key_required_until,resolution,actor_audit_ref,
              authz_snapshot_digest,policy_code,created_at,deletion_digest
            ) VALUES ('del_1','t_old',1,'recovery_pending',1,0,0,NULL,NULL,
              '{}','{}',1,0,'effect_identity','effect_bundle','key_1','future',
              'fenced_deleted','actor','auth','policy','t','deletion_digest')
            """,
        )
        connection.commit()
    blocked = await _s1_store(db_path).admit_or_steer_v1(_admission_request())
    assert blocked.code is AdmissionCode.SESSION_PRIOR_EFFECT_UNKNOWN
    assert _protocol_counts(db_path)["turns"] == 1


@pytest.mark.asyncio
async def test_terminal_protocol_reader_rejects_future_and_malformed_rows(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id="s_future")
        connection.execute(
            """
            INSERT INTO turns(id,session_id,seq,user_text,status,started_at,terminal_protocol_version)
            VALUES ('t_future','s_future',1,'future','running','t',2)
            """,
        )
        _insert_base(connection, session_id="s_malformed")
        connection.execute(
            """
            INSERT INTO turns(
              id,session_id,seq,user_text,status,started_at,terminal_protocol_version,
              terminal_ingress_surface,terminal_admission_cohort,protocol_admitted_at,
              current_generation,generation_transition_epoch,event_sequence_next,
              delivery_fence_epoch,presentation_locale,message_catalog_version
            ) VALUES ('t_malformed','s_malformed',1,'bad','running','t',1,'web','bucket','t',1,1,1,0,'zh-CN',1)
            """,
        )
        connection.commit()

    store = TerminalProtocolStore(db_path)
    with pytest.raises(TerminalProtocolFutureVersionError):
        await store.read_protocol_summary(_context(session_id="s_future", turn_id="t_future"))
    with pytest.raises(TerminalProtocolMalformedError, match="current generation"):
        await store.read_protocol_summary(_context(session_id="s_malformed", turn_id="t_malformed"))


@pytest.mark.asyncio
async def test_web_start_action_lookup_is_exact_owner_scoped_and_read_only(db_path: str) -> None:
    action_id = "act_start_0123456789abcdef0123456789abcdef"
    session_id = "s_0123456789abcdef0123456789abcdef"
    turn_id = "t_0123456789abcdef0123456789abcdef"
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id=session_id)
        _insert_writer_lease(connection)
        connection.commit()
    store = _s1_store(db_path)
    admitted = await store.admit_or_steer_v1(
        _typed_admission_request(
            request_id=action_id,
            turn_id=turn_id,
            session_id=session_id,
        )
    )
    assert admitted.disposition is AdmissionDisposition.ADMITTED
    before = _protocol_counts(db_path)

    found = await store.read_web_chat_action(
        action_id=action_id,
        action_kind="start",
        actor="u_1",
        team_id="team_1",
        turn_id=turn_id,
    )

    assert found is not None
    assert found.action_id == action_id
    assert found.action_kind == "start"
    assert found.scope == TerminalScope(
        team_id="team_1",
        session_id=session_id,
        turn_id=turn_id,
        generation=1,
    )
    assert found.workspace_id == "ws_1"
    assert found.request_digest == admitted.request_digest
    assert found.paradigm_v2 is None
    assert (
        await store.read_web_chat_action(
            action_id=action_id,
            action_kind="start",
            actor="u_other",
            team_id="team_1",
            turn_id=turn_id,
        )
        is None
    )
    assert (
        await store.read_web_chat_action(
            action_id=action_id,
            action_kind="start",
            actor="u_1",
            team_id="team_other",
            turn_id=turn_id,
        )
        is None
    )
    assert (
        await store.read_web_chat_action(
            action_id="act_start_ffffffffffffffffffffffffffffffff",
            action_kind="start",
            actor="u_1",
            team_id="team_1",
            turn_id=turn_id,
        )
        is None
    )
    assert _protocol_counts(db_path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("paradigm_v2", (False, True))
async def test_web_start_action_lookup_reads_explicit_paradigm_snapshot(
    db_path: str,
    paradigm_v2: bool,
) -> None:
    action_id = "act_start_0123456789abcdef0123456789abcdef"
    session_id = "s_0123456789abcdef0123456789abcdef"
    turn_id = "t_0123456789abcdef0123456789abcdef"
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id=session_id)
        _insert_writer_lease(connection)
        connection.commit()
    request = _typed_admission_request(
        request_id=action_id,
        turn_id=turn_id,
        session_id=session_id,
    )
    request = replace(
        request,
        execution_intent={
            **request.execution_intent,
            "paradigm_v2": paradigm_v2,
        },
    )
    store = _s1_store(db_path)
    admitted = await store.admit_or_steer_v1(request)

    found = await store.read_web_chat_action(
        action_id=action_id,
        action_kind="start",
        actor="u_1",
        team_id="team_1",
        turn_id=turn_id,
    )

    assert found is not None
    assert found.paradigm_v2 is paradigm_v2
    assert found.request_digest == admitted.request_digest


@pytest.mark.asyncio
async def test_web_start_action_lookup_reads_persisted_language_snapshot(db_path: str) -> None:
    action_id = "act_start_0123456789abcdef0123456789abcdef"
    session_id = "s_0123456789abcdef0123456789abcdef"
    turn_id = "t_0123456789abcdef0123456789abcdef"
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id=session_id)
        _insert_writer_lease(connection)
        connection.commit()
    language_context = LanguageContext(ui_language="fr-FR", output_language="de-DE").to_dict()
    request = _typed_admission_request(
        request_id=action_id,
        turn_id=turn_id,
        session_id=session_id,
    )
    request = replace(
        request,
        presentation_locale="fr-FR",
        execution_intent={
            **request.execution_intent,
            "language_context": language_context,
        },
    )
    store = _s1_store(db_path)
    admitted = await store.admit_or_steer_v1(request)
    assert admitted.disposition is AdmissionDisposition.ADMITTED

    found = await store.read_web_chat_action(
        action_id=action_id,
        action_kind="start",
        actor="u_1",
        team_id="team_1",
        turn_id=turn_id,
    )

    assert found is not None
    assert found.language_context == language_context
    assert found.request_digest == admitted.request_digest


@pytest.mark.asyncio
@pytest.mark.parametrize("source_version", ["legacy", "current"])
async def test_web_start_action_lookup_reads_persisted_resolution_snapshot(db_path: str, source_version: str) -> None:
    action_id = "act_start_0123456789abcdef0123456789abcdef"
    session_id = "s_0123456789abcdef0123456789abcdef"
    turn_id = "t_0123456789abcdef0123456789abcdef"
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id=session_id)
        _insert_writer_lease(connection)
        connection.commit()
    resolution = {
        "active_datarc_project": "project_1",
        "member_uid": "member_1",
        "preferred_model": "model_1",
        "preferred_provider": "provider_1",
    }
    if source_version == "current":
        from bowhead_core.datarc_selection import DatarcSelectionSnapshot

        resolution["datarc_selection"] = DatarcSelectionSnapshot(
            state="selected",
            team_id="team_1",
            user_id="u_1",
            connection_id="dc_1",
            connection_revision="1",
            datarc_workspace_id="project_1",
            member_uid="member_1",
        ).model_dump()
    request = _typed_admission_request(
        request_id=action_id,
        turn_id=turn_id,
        session_id=session_id,
    )
    request = replace(
        request,
        execution_intent={**request.execution_intent, "paradigm_v2": False, **resolution},
    )
    store = _s1_store(db_path)
    admitted = await store.admit_or_steer_v1(request)
    assert admitted.disposition is AdmissionDisposition.ADMITTED

    found = await store.read_web_chat_action(
        action_id=action_id,
        action_kind="start",
        actor="u_1",
        team_id="team_1",
        turn_id=turn_id,
    )

    assert found is not None
    assert dict(found.admitted_resolution or {}) == resolution
    assert found.request_digest == admitted.request_digest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corrupt_source_sql",
    [
        "DELETE FROM chat_turn_user_sources WHERE turn_id='t_0123456789abcdef0123456789abcdef'",
        "UPDATE chat_turn_user_sources SET user_text='changed' WHERE turn_id='t_0123456789abcdef0123456789abcdef'",
        "UPDATE chat_turn_user_sources SET attachment_ids_json='[\"changed\"]' WHERE turn_id='t_0123456789abcdef0123456789abcdef'",
        "UPDATE chat_turn_user_sources SET attachment_refs_json='[{\"artifact_id\":true}]' WHERE turn_id='t_0123456789abcdef0123456789abcdef'",
        "UPDATE turn_generations SET execution_intent_digest='changed' WHERE turn_id='t_0123456789abcdef0123456789abcdef' AND generation=1",
        "UPDATE turn_generations SET admission_request_digest='changed' WHERE turn_id='t_0123456789abcdef0123456789abcdef' AND generation=1",
        "UPDATE turns SET presentation_locale='' WHERE id='t_0123456789abcdef0123456789abcdef'",
        "UPDATE turns SET presentation_locale='en-US' WHERE id='t_0123456789abcdef0123456789abcdef'",
        "UPDATE turns SET message_catalog_version=0 WHERE id='t_0123456789abcdef0123456789abcdef'",
    ],
)
async def test_web_start_action_lookup_rejects_missing_or_changed_source(
    db_path: str,
    corrupt_source_sql: str,
) -> None:
    action_id = "act_start_0123456789abcdef0123456789abcdef"
    session_id = "s_0123456789abcdef0123456789abcdef"
    turn_id = "t_0123456789abcdef0123456789abcdef"
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id=session_id)
        _insert_writer_lease(connection)
        connection.commit()
    store = _s1_store(db_path)
    admitted = await store.admit_or_steer_v1(
        _typed_admission_request(
            request_id=action_id,
            turn_id=turn_id,
            session_id=session_id,
        )
    )
    assert admitted.disposition is AdmissionDisposition.ADMITTED
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TRIGGER IF EXISTS turn_generation_identity_immutable")
        connection.execute("DROP TRIGGER IF EXISTS terminal_generation_envelope_immutable")
        connection.execute("DROP TRIGGER IF EXISTS turns_terminal_protocol_immutable")
        connection.execute("DROP TRIGGER IF EXISTS turns_terminal_protocol_shape_update")
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(corrupt_source_sql)
        connection.commit()

    with pytest.raises(WebChatActionMalformedError, match="malformed"):
        await store.read_web_chat_action(
            action_id=action_id,
            action_kind="start",
            actor="u_1",
            team_id="team_1",
            turn_id=turn_id,
        )


@pytest.mark.asyncio
async def test_typed_start_replay_and_action_lookup_reject_incomplete_execution_context(
    db_path: str,
) -> None:
    request = _typed_admission_request()
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id=request.context.session_id)
        _insert_writer_lease(connection)
        connection.commit()
    store = _s1_store(db_path)
    admitted = await store.admit_or_steer_v1(request)
    assert admitted.disposition is AdmissionDisposition.ADMITTED

    malformed_intent = dict(request.execution_intent)
    malformed_intent.pop("member_uid")
    malformed_request = replace(
        request,
        execution_intent=malformed_intent,
    )
    malformed_digest = canonical_digest_v1(
        {
            "execution_intent": malformed_intent,
            "request_id": request.context.request_id,
            "session_id": request.context.session_id,
            "team_id": request.context.team_id,
            "turn_id": request.turn_id,
            "user_text": request.user_text,
        }
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "DROP TRIGGER IF EXISTS turn_generation_identity_immutable",
        )
        connection.execute(
            "DROP TRIGGER IF EXISTS terminal_generation_envelope_immutable",
        )
        connection.execute(
            """
            UPDATE turn_generations
            SET execution_intent_json=?,execution_intent_digest=?,
                admission_request_digest=?
            WHERE turn_id=? AND generation=1
            """,
            (
                canonical_json_v1(malformed_intent),
                malformed_digest,
                malformed_digest,
                request.turn_id,
            ),
        )
        connection.commit()

    with pytest.raises(
        TerminalProtocolMalformedError,
        match="execution context is malformed",
    ):
        await store.admit_or_steer_v1(malformed_request)
    with pytest.raises(
        WebChatActionMalformedError,
        match="execution context is malformed",
    ):
        await store.read_web_chat_action(
            action_id=request.context.request_id,
            action_kind="start",
            actor=request.context.actor,
            team_id=request.context.team_id,
            turn_id=request.turn_id,
        )


@pytest.mark.asyncio
async def test_web_steer_action_lookup_accepts_both_durable_states_and_exact_tuple(db_path: str) -> None:
    action_id = "act_steer_0123456789abcdef0123456789abcdef"
    user_message_id = "u_r_0123456789abcdef0123456789abcdef"
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()
    store = _s1_store(db_path)
    context = await _start_execution(store)
    proposal = await store.accept_event(
        context,
        event_type="emoji_ack",
        actor_scope=ActorScope.ROOT,
        phase_scope=PhaseScope.IN_GENERATION,
        payload={
            "emoji": "🤝",
            "steer_id": action_id,
            "user_msg_id": user_message_id,
        },
        idempotency_key=f"steer_ack:{action_id}",
    )
    accepted_counts = _protocol_counts(db_path)

    accepted = await store.read_web_chat_action(
        action_id=action_id,
        action_kind="steer",
        actor="u_1",
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        execution_generation=1,
        user_message_id=user_message_id,
    )

    assert accepted is not None
    assert accepted.action_id == action_id
    assert accepted.action_kind == "steer"
    assert accepted.scope == TerminalScope(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        generation=1,
    )
    assert accepted.workspace_id == "ws_1"
    assert accepted.request_digest is None
    assert (
        await store.read_web_chat_action(
            action_id=action_id,
            action_kind="steer",
            actor="u_1",
            team_id="team_1",
            session_id="s_1",
            turn_id="t_1",
            execution_generation=2,
            user_message_id=user_message_id,
        )
        is None
    )
    assert (
        await store.read_web_chat_action(
            action_id=action_id,
            action_kind="steer",
            actor="u_other",
            team_id="team_1",
            session_id="s_1",
            turn_id="t_1",
            execution_generation=1,
            user_message_id=user_message_id,
        )
        is None
    )
    assert (
        await store.read_web_chat_action(
            action_id=action_id,
            action_kind="steer",
            actor="u_1",
            team_id="team_other",
            session_id="s_1",
            turn_id="t_1",
            execution_generation=1,
            user_message_id=user_message_id,
        )
        is None
    )
    assert _protocol_counts(db_path) == accepted_counts

    await store.flush_through(context, accepted_through=proposal.accept_ordinal)
    persisted_counts = _protocol_counts(db_path)
    persisted = await store.read_web_chat_action(
        action_id=action_id,
        action_kind="steer",
        actor="u_1",
        team_id="team_1",
        session_id="s_1",
        turn_id="t_1",
        execution_generation=1,
        user_message_id=user_message_id,
    )
    assert persisted == accepted
    assert _protocol_counts(db_path) == persisted_counts


@pytest.mark.parametrize(
    ("persisted", "error_match"),
    (
        (False, "proposal allocator"),
        (True, "event allocator"),
    ),
)
@pytest.mark.asyncio
async def test_web_steer_action_lookup_rejects_uncommitted_allocator_state(
    db_path: str,
    persisted: bool,
    error_match: str,
) -> None:
    action_id = "act_steer_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    user_message_id = "u_r_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    proposal_id, event_id = _idempotent_event_identity(
        turn_id="t_1",
        generation=1,
        idempotency_key=f"steer_ack:{action_id}",
    )
    payload = {
        "emoji": "🤝",
        "steer_id": action_id,
        "user_msg_id": user_message_id,
    }
    payload_json = canonical_json_v1(payload)
    payload_digest = canonical_digest_v1(payload)
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_base(connection)
        _insert_v1_open(connection)
        connection.execute(
            """
            UPDATE turn_generations
            SET claim_epoch=1,accepted_proposal_next=?
            WHERE turn_id='t_1' AND generation=1
            """,
            (2 if persisted else 1,),
        )
        connection.execute(
            """
            INSERT INTO turn_event_proposals(
              proposal_id,event_id,turn_id,generation,accept_ordinal,
              accepted_claim_epoch,event_type,actor_scope,phase_scope,
              payload_json,payload_digest,durability,state,accepted_at,
              persisted_at,persisted_event_seq
            ) VALUES (?,?, 't_1',1,1,1,'emoji_ack','root','in_generation',
                      ?,?,'committed',?,'accepted-at',?,?)
            """,
            (
                proposal_id,
                event_id,
                payload_json,
                payload_digest,
                "persisted" if persisted else "accepted",
                "persisted-at" if persisted else None,
                1 if persisted else None,
            ),
        )
        if persisted:
            connection.execute(
                """
                INSERT INTO turn_events(
                  id,turn_id,seq,event_type,payload_json,occurred_at,
                  protocol_version,generation,event_seq,actor_scope,phase_scope,
                  payload_digest,source_proposal_id,accept_ordinal
                ) VALUES (?, 't_1',1,'emoji_ack',?,'accepted-at',1,1,1,
                          'root','in_generation',?,?,1)
                """,
                (event_id, payload_json, payload_digest, proposal_id),
            )
        connection.commit()

    with pytest.raises(WebChatActionMalformedError, match=error_match) as exc_info:
        await TerminalProtocolStore(db_path).read_web_chat_action(
            action_id=action_id,
            action_kind="steer",
            actor="u_1",
            team_id="team_1",
            session_id="s_1",
            turn_id="t_1",
            execution_generation=1,
            user_message_id=user_message_id,
        )
    assert exc_info.value.access_scope is not None
    assert exc_info.value.access_scope.workspace_id == "ws_1"


@pytest.mark.unit
def test_web_steer_action_event_identity_known_vector() -> None:
    assert _idempotent_event_identity(
        turn_id="t_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        generation=1,
        idempotency_key=("steer_ack:act_steer_0123456789abcdef0123456789abcdef"),
    ) == (
        "tep_a201033b283df9aacc01492005e7f4b6",
        "evt_a201033b283df9aacc01492005e7f4b6",
    )


def test_web_action_lookup_is_primary_key_read_only_sql(db_path: str) -> None:
    import ast
    import inspect
    import textwrap

    start_source = inspect.getsource(TerminalProtocolStore._read_web_start_action)
    steer_source = inspect.getsource(TerminalProtocolStore._read_web_steer_action)
    combined = f"{start_source}\n{steer_source}".lower()

    assert "where t.id=?" in combined
    assert "where p.proposal_id=?" in combined
    for forbidden in (
        "where g.execution_intent_id=?",
        "json_each",
        "json_extract",
        "insert ",
        "update ",
        "delete ",
        "begin ",
    ):
        assert forbidden not in combined
    assert combined.count('execute("pragma query_only=on")') == 2

    def _select(source: str) -> str:
        tree = ast.parse(textwrap.dedent(source))
        matches = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "SELECT" in node.value and "FROM" in node.value
        ]
        assert len(matches) == 1
        return matches[0]

    with sqlite3.connect(db_path) as connection:
        start_plan = connection.execute(
            f"EXPLAIN QUERY PLAN {_select(start_source)}",
            ("t_missing", "u_1", "team_1"),
        ).fetchall()
        steer_plan = connection.execute(
            f"EXPLAIN QUERY PLAN {_select(steer_source)}",
            (
                "tep_missing",
                "t_missing",
                1,
                "s_missing",
                "u_1",
                "team_1",
            ),
        ).fetchall()
    start_details = [str(row[3]) for row in start_plan]
    steer_details = [str(row[3]) for row in steer_plan]
    assert any("SEARCH t USING INDEX" in detail and "(id=?)" in detail for detail in start_details)
    assert any("SEARCH p USING INDEX" in detail and "(proposal_id=?)" in detail for detail in steer_details)
    assert not any("SCAN " in detail for detail in (*start_details, *steer_details))


def test_typed_start_admission_replay_is_exact_turn_primary_key_sql() -> None:
    import inspect

    source = inspect.getsource(_read_start_admission_replay).lower()
    assert "where t.id=?" in source
    assert "where g.execution_intent_id=?" not in source
    assert "json_each" not in source
    assert "json_extract" not in source


@pytest.mark.asyncio
async def test_web_start_action_lookup_classifies_future_and_malformed_protocols(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id="s_future_action")
        connection.execute(
            """
            INSERT INTO turns(
              id,session_id,seq,user_text,status,started_at,
              terminal_protocol_version,terminal_ingress_surface,current_generation
            ) VALUES ('t_future_action','s_future_action',1,'future','running','t',2,'web',1)
            """,
        )
        connection.execute(
            """
            INSERT INTO turn_generations(
              turn_id,generation,protocol_version,kind,execution_intent_id,
              execution_intent_digest,opened_at,state
            ) VALUES (
              't_future_action',1,1,'execution',
              'act_start_11111111111111111111111111111111','digest','t','opened'
            )
            """,
        )
        _insert_base(connection, session_id="s_malformed_action")
        connection.execute(
            """
            INSERT INTO turns(
              id,session_id,seq,user_text,status,started_at,
              terminal_protocol_version,terminal_ingress_surface,current_generation
            ) VALUES (
              't_malformed_action','s_malformed_action',1,'malformed','running','t',
              'broken','web',1
            )
            """,
        )
        connection.execute(
            """
            INSERT INTO turn_generations(
              turn_id,generation,protocol_version,kind,execution_intent_id,
              execution_intent_digest,opened_at,state
            ) VALUES (
              't_malformed_action',1,1,'execution',
              'act_start_22222222222222222222222222222222','digest','t','opened'
            )
            """,
        )
        _insert_base(connection, session_id="s_missing_generation_action")
        connection.execute(
            """
            INSERT INTO turns(
              id,session_id,seq,user_text,status,started_at,
              terminal_protocol_version,terminal_ingress_surface,
              terminal_admission_cohort,protocol_admitted_at,current_generation,
              generation_transition_epoch,event_sequence_next,delivery_fence_epoch,
              presentation_locale,message_catalog_version
            ) VALUES (
              't_missing_generation_action','s_missing_generation_action',1,
              'missing','running','t',1,'web','bucket','t',1,1,1,0,'zh-CN',1
            )
            """,
        )
        connection.commit()
    store = _s1_store(db_path)
    before = _protocol_counts(db_path)

    with pytest.raises(WebChatActionFutureVersionError, match="future") as future_info:
        await store.read_web_chat_action(
            action_id="act_start_11111111111111111111111111111111",
            action_kind="start",
            actor="u_1",
            team_id="team_1",
            turn_id="t_future_action",
        )
    assert future_info.value.access_scope is not None
    assert future_info.value.access_scope.workspace_id == "ws_1"
    with pytest.raises(WebChatActionMalformedError, match="malformed") as malformed_info:
        await store.read_web_chat_action(
            action_id="act_start_22222222222222222222222222222222",
            action_kind="start",
            actor="u_1",
            team_id="team_1",
            turn_id="t_malformed_action",
        )
    assert malformed_info.value.access_scope is not None
    assert malformed_info.value.access_scope.workspace_id == "ws_1"
    with pytest.raises(WebChatActionMalformedError, match="malformed") as missing_info:
        await store.read_web_chat_action(
            action_id="act_start_33333333333333333333333333333333",
            action_kind="start",
            actor="u_1",
            team_id="team_1",
            turn_id="t_missing_generation_action",
        )
    assert missing_info.value.access_scope is not None
    assert missing_info.value.access_scope.workspace_id == "ws_1"
    assert _protocol_counts(db_path) == before


@pytest.mark.asyncio
async def test_s2_durable_accept_survives_fault_before_runtime_enqueue(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()
    context = await _start_execution(_s1_store(db_path))

    faulted = _s1_store(db_path, fault=TerminalProtocolFaultPoint.EVENT_AFTER_DURABLE_ACCEPT)
    with pytest.raises(RuntimeError, match="EVENT_AFTER_DURABLE_ACCEPT"):
        await faulted.accept_event(
            context,
            event_type="step_started",
            actor_scope=ActorScope.STEP,
            phase_scope=PhaseScope.IN_GENERATION,
            payload={"step_id": "step_1"},
        )

    with sqlite3.connect(db_path) as connection:
        proposal = connection.execute(
            "SELECT accept_ordinal,state,payload_json FROM turn_event_proposals",
        ).fetchone()
        accepted_next = connection.execute(
            "SELECT accepted_proposal_next FROM turn_generations WHERE turn_id='t_1'",
        ).fetchone()
        event_count = connection.execute("SELECT COUNT(*) FROM turn_events").fetchone()
    assert proposal == (1, "accepted", '{"step_id":"step_1"}')
    assert accepted_next == (2,)
    assert event_count == (0,)

    tickets = await _s1_store(db_path).flush_through(context, accepted_through=1)
    assert [(ticket.accept_ordinal, ticket.event_seq, ticket.legacy_seq) for ticket in tickets] == [(1, 1, 1)]


@pytest.mark.asyncio
async def test_s2_accept_rejects_transport_only_root_error_before_writing(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()
    context = await _start_execution(_s1_store(db_path))

    with pytest.raises(ValueError, match="transport-only"):
        await _s1_store(db_path).accept_event(
            context,
            event_type="error",
            actor_scope=ActorScope.ROOT,
            phase_scope=PhaseScope.IN_GENERATION,
            payload={"durability": "non_durable"},
            root_durability=RootDurability.NON_DURABLE,
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_event_proposals").fetchone() == (0,)


@pytest.mark.asyncio
async def test_s2_materialize_prefix_is_atomic_immutable_and_idempotent(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()
    store = _s1_store(db_path)
    context = await _start_execution(store)
    mutable_payload = {"text": "original", "nested": {"value": 1}}
    first = await store.accept_event(
        context,
        event_type="narrative.delta",
        actor_scope=ActorScope.ROOT,
        phase_scope=PhaseScope.IN_GENERATION,
        payload=mutable_payload,
    )
    mutable_payload["text"] = "mutated"
    second = await store.accept_event(
        context,
        event_type="step_done",
        actor_scope=ActorScope.STEP,
        phase_scope=PhaseScope.IN_GENERATION,
        payload={"step_id": "step_1", "ok": True},
        publish_mode=EventPublishMode.BEFORE_LIVE,
    )

    tickets = await store.flush_through(context, accepted_through=2)
    replay = await store.flush_through(context, accepted_through=2)
    assert tickets == replay
    assert [ticket.event_id for ticket in tickets] == [first.event_id, second.event_id]
    assert [ticket.event_seq for ticket in tickets] == [1, 2]
    assert tickets[0].payload() == {"nested": {"value": 1}, "text": "original"}
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT e.id,e.seq,e.event_seq,e.source_proposal_id,p.state,p.persisted_event_seq
            FROM turn_events e JOIN turn_event_proposals p ON p.proposal_id=e.source_proposal_id
            ORDER BY e.event_seq
            """,
        ).fetchall()
        allocator = connection.execute("SELECT event_sequence_next FROM turns WHERE id='t_1'").fetchone()
    assert rows == [
        (first.event_id, 1, 1, first.proposal_id, "persisted", 1),
        (second.event_id, 2, 2, second.proposal_id, "persisted", 2),
    ]
    assert allocator == (3,)


@pytest.mark.asyncio
async def test_s2_materialize_faults_are_all_or_nothing_and_ack_loss_is_idempotent(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()
    store = _s1_store(db_path)
    context = await _start_execution(store)
    proposal = await store.accept_event(
        context,
        event_type="step_started",
        actor_scope=ActorScope.STEP,
        phase_scope=PhaseScope.IN_GENERATION,
        payload={"step_id": "step_1"},
    )

    before = _s1_store(db_path, fault=TerminalProtocolFaultPoint.EVENT_BEFORE_PERSIST)
    with pytest.raises(RuntimeError, match="EVENT_BEFORE_PERSIST"):
        await before.flush_through(context, accepted_through=1)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT state FROM turn_event_proposals").fetchone() == ("accepted",)
        assert connection.execute("SELECT COUNT(*) FROM turn_events").fetchone() == (0,)
        assert connection.execute("SELECT event_sequence_next FROM turns WHERE id='t_1'").fetchone() == (1,)

    after = _s1_store(db_path, fault=TerminalProtocolFaultPoint.EVENT_AFTER_PERSIST)
    with pytest.raises(RuntimeError, match="EVENT_AFTER_PERSIST"):
        await after.flush_through(context, accepted_through=1)
    tickets = await store.flush_through(context, accepted_through=1)
    assert [(ticket.event_id, ticket.event_seq) for ticket in tickets] == [(proposal.event_id, 1)]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_events").fetchone() == (1,)


@pytest.mark.asyncio
async def test_ac_b1b_032_db_log_gap_does_not_reuse_event_seq(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()
    store = _s1_store(db_path)
    context = await _start_execution(store)
    await store.accept_event(
        context,
        event_type="step_started",
        actor_scope=ActorScope.STEP,
        phase_scope=PhaseScope.IN_GENERATION,
        payload={"step_id": "step_1"},
    )
    await store.flush_through(context, accepted_through=1)
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE turns SET event_sequence_next=5 WHERE id='t_1'")
        connection.commit()
    second = await store.accept_event(
        context,
        event_type="step_done",
        actor_scope=ActorScope.STEP,
        phase_scope=PhaseScope.IN_GENERATION,
        payload={"step_id": "step_1", "ok": True},
    )
    tickets = await store.flush_through(context, accepted_through=2)
    assert tickets[-1].event_id == second.event_id
    assert tickets[-1].event_seq == 5
    with pytest.raises(TerminalProtocolMalformedError, match="sequence gap"):
        await store.read_checkpoint(_context())


@pytest.mark.asyncio
async def test_ac_b1b_050_late_old_generation_event_is_fenced(db_path: str) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection)
        _insert_writer_lease(connection)
        connection.commit()
    store = _s1_store(db_path)
    context = await _start_execution(store)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE turn_generations SET state='closing',closing_accepted_through=0,row_version=row_version+1
            WHERE turn_id='t_1' AND generation=1
            """,
        )
        connection.commit()

    with pytest.raises(TerminalProtocolMalformedError, match="ownership fence"):
        await store.accept_event(
            context,
            event_type="late",
            actor_scope=ActorScope.STEP,
            phase_scope=PhaseScope.IN_GENERATION,
            payload={},
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT accepted_proposal_next FROM turn_generations").fetchone() == (1,)


@pytest.mark.asyncio
async def test_exact_draft_start_survives_reopen_and_rejects_retargeting(db_path: str) -> None:
    request = _typed_admission_request()
    target = {"kind": "agent_draft", "id": "sad_original", "version": 3}
    request = replace(request, execution_intent={**request.execution_intent, "agent_draft_target": target})
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id=request.context.session_id)
        _insert_writer_lease(connection)
        connection.commit()
    admitted = await _s1_store(db_path).admit_or_steer_v1(request)
    assert admitted.disposition is AdmissionDisposition.ADMITTED
    reopened = _s1_store(db_path)
    found = await reopened.read_web_chat_action(
        action_id=request.context.request_id, action_kind="start", actor="u_1", team_id="team_1", turn_id=request.turn_id
    )
    assert found is not None and dict(found.agent_draft_target or {}) == target
    replay = await reopened.admit_or_steer_v1(request)
    assert replay.request_digest == admitted.request_digest
    for replacement in ({**target, "id": "sad_other"}, {**target, "version": 4}):
        rejected = await reopened.admit_or_steer_v1(
            replace(request, execution_intent={**request.execution_intent, "agent_draft_target": replacement})
        )
        assert rejected.disposition is AdmissionDisposition.REJECTED
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM turn_generations WHERE turn_id=?", (request.turn_id,)).fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        None,
        {"kind": "agent_draft", "id": "sad_a", "version": True},
        {"kind": "agent_draft", "id": "sad_a", "version": 0},
        {"kind": "agent_draft", "id": "sad_a", "version": 1, "grant": "write"},
    ],
)
async def test_draft_start_rejects_malformed_persisted_target_before_admission(db_path: str, target) -> None:
    request = _typed_admission_request()
    request = replace(request, execution_intent={**request.execution_intent, "agent_draft_target": target})
    with sqlite3.connect(db_path) as connection:
        _insert_base(connection, session_id=request.context.session_id)
        _insert_writer_lease(connection)
        connection.commit()
    result = await _s1_store(db_path).admit_or_steer_v1(request)
    assert result.disposition is AdmissionDisposition.REJECTED
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM turn_generations").fetchone()[0] == 0
