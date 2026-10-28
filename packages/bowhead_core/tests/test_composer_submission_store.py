"""Composer batch-A durable submission protocol tests."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import bowhead_core.session_store as session_store_module
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.chat_turn_source import ReferenceExecutionUnavailableError
from bowhead_core.events import (
    EventBusRegistry,
    OrchestratorEvent,
    TerminalDurabilityAbort,
)
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.plan_store import PlanStatus, PlanStore
from bowhead_core.session_store import SessionProtocolManagedError, SessionStore
from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore


def _chain_head() -> str:
    """共享 revision 链当前的 head（从脚本目录读，不写死号）。

    与 `test_local_oidc_store._chain_head` 同源——那条测试先撞的同一颗雷。
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from bowhead_core import migrations

    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"revision 链分叉了，heads={heads}——按迁移规范应以 merge revision 合并"
    return heads[0]


def _finalize_args(common: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in common.items()
        if key
        not in {
            "accepted_request_fingerprints",
            "fingerprint_key_id",
            "auth_session_generation_hash",
        }
    }


async def _record_root_done(
    db_path: str,
    *,
    turn_id: str,
    status: str,
    execution_generation: int = 1,
    final_answer: str = "",
) -> None:
    log_store = TurnLogStore(db_path)
    await log_store.initialize()
    next_seq = await log_store.max_seq(turn_id) + 1
    if final_answer:
        recorded_answer = await log_store.record(
            turn_id=turn_id,
            event_type="final_answer",
            payload={
                "turn_id": turn_id,
                "text": final_answer,
                "execution_generation": execution_generation,
                "seq": next_seq,
                "evt_seq": next_seq,
            },
        )
        assert recorded_answer is not None
        next_seq += 1
    recorded = await log_store.record(
        turn_id=turn_id,
        event_type="done",
        payload={
            "turn_id": turn_id,
            "status": status,
            "execution_generation": execution_generation,
            "seq": next_seq,
            "evt_seq": next_seq,
        },
    )
    assert recorded is not None


def _record_completed_clarification_decision(
    db_path: str,
    *,
    turn_id: str,
    request_id: str,
    payload: dict[str, object],
) -> None:
    """Persist the two canonical facts required before receipt delivery."""
    now = datetime.now(tz=UTC).isoformat()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO clarification_requests("
            "request_id,turn_id,user_id,workspace_id,token,rationale,"
            "candidates_json,status,chosen_value,chosen_at,created_at,kind,"
            "multi_select) VALUES (?,?,?,?,?,?,?,'resolved',?,?,?,?,0)",
            (
                request_id,
                turn_id,
                "u_1",
                "ws_1",
                "token",
                "test",
                "[]",
                str(payload.get("chosen_value", "")),
                now,
                now,
                "token",
            ),
        )
        connection.execute(
            "INSERT INTO clarification_resolution_outbox("
            "request_id,turn_id,state,channel,work_json,payload_json,"
            "lease_owner,lease_expires_at,attempt_count,created_at,updated_at) "
            "VALUES (?,?,'completed','web','{}',?,NULL,NULL,1,?,?)",
            (
                request_id,
                turn_id,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
        connection.commit()


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = tmp_path / "bowhead.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    upgrade(f"sqlite+aiosqlite:///{path}", target="core")
    now = datetime.now(tz=UTC).isoformat()
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT INTO chat_auth_session_generations("
            "user_id,auth_session_generation_hash,state,created_at,invalidated_at"
            ") VALUES (?,?,'active',?,NULL)",
            [("u_1", digit * 64, now) for digit in ("0", "1", "2")],
        )
        connection.commit()
    return str(path)


def test_resume_dispatch_migration_adds_generation_lease_fence(
    db_path: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        turn_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(turns)",
            ).fetchall()
        }
        assert {
            "execution_generation",
            "generation_base_tokens_input",
            "generation_base_tokens_output",
            "generation_base_cost_usd",
        } <= turn_columns
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(chat_turn_spawn_dispatch)",
            ).fetchall()
        }
        assert {"lease_kind", "lease_execution_generation"} <= columns
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO chat_turn_spawn_dispatch("
                "turn_id,session_id,source_submission_ids_json,dispatch_kind,"
                "state,lease_kind,lease_execution_generation,lease_expires_at,"
                "created_at,updated_at) VALUES ("
                "'missing','s','[]','start','claimed','invalid',0,'x','x','x')",
            )


@pytest.mark.asyncio
async def test_submission_schema_probe_accepts_head_and_rejects_altered_tombstone(db_path: str) -> None:
    # Head carries the account-purge guard that 0179 adds to the cancel-receipt tombstone.
    store = SessionStore(db_path)
    assert await store.chat_submission_protocol_schema_ready() is True
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            "DROP TRIGGER trg_turn_delete_cancel_receipt_tombstone;"
            "CREATE TRIGGER trg_turn_delete_cancel_receipt_tombstone BEFORE DELETE ON turns "
            "BEGIN UPDATE turn_cancel_receipts SET turn_erased=0 WHERE turn_id=OLD.id; END;"
        )
    assert await store.chat_submission_protocol_schema_ready() is False


@pytest.mark.asyncio
async def test_same_submission_replays_one_durable_turn(db_path: str) -> None:
    store = SessionStore(db_path)
    common = {
        "team_id": "team_1",
        "user_id": "u_1",
        "client_submission_id": "018f0cc3-e4a2-7b21-a109-001122334455",
        "workspace_id": "ws_1",
        "requested_session_id": None,
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "request_fingerprint": "a" * 64,
        "accepted_request_fingerprints": ("a" * 64,),
        "fingerprint_key_id": "f" * 64,
        "auth_session_generation_hash": "0" * 64,
    }
    claim = await store.claim_chat_submission(**common)
    assert claim.kind == "claimed"
    response = {
        "session_id": "s_actual",
        "turn_id": "t_once",
        "stream_url": "/api/v1/chat/stream/t_once",
        "status": "started",
        "execution_generation": 1,
    }
    receipt = await store.finalize_started_chat_submission(
        **_finalize_args(common),
        actual_session_id="s_actual",
        turn_id="t_once",
        user_text="只执行一次",
        attachment_ids=[],
        attachment_refs=[],
        lease_owner=claim.lease_owner,
        lease_version=claim.lease_version,
        response=response,
    )
    assert receipt.response == response

    replay = await store.claim_chat_submission(**common)
    assert replay.kind == "receipt"
    assert replay.receipt is not None and replay.receipt.response == response
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turns WHERE id='t_once'").fetchone() == (1,)
        # Assert upgrade reached the chain head, without hardcoding a revision number, which makes **every new migration**
        # fail this test (it remained failing after `0021_artifact_origin_query` landed).
        # This test proves durable submission creates one Turn and fully upgrades the database; the head's number is irrelevant.
        # Same fix as test_local_oidc_store._chain_head (hit earlier there; the criterion shares the same source).
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (_chain_head(),)


@pytest.mark.asyncio
async def test_submission_id_conflict_and_cross_scope_are_fail_closed(db_path: str) -> None:
    store = SessionStore(db_path)
    common = {
        "team_id": "team_1",
        "user_id": "u_1",
        "client_submission_id": "018f0cc3-e4a2-7b21-a109-001122334466",
        "workspace_id": "ws_1",
        "requested_session_id": "s_requested",
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "request_fingerprint": "b" * 64,
        "accepted_request_fingerprints": ("b" * 64,),
        "fingerprint_key_id": "f" * 64,
        "auth_session_generation_hash": "0" * 64,
    }
    assert (await store.claim_chat_submission(**common)).kind == "claimed"
    assert (
        await store.claim_chat_submission(
            **{
                **common,
                "request_fingerprint": "c" * 64,
                "accepted_request_fingerprints": ("c" * 64,),
            },
        )
    ).kind == "conflict"
    assert (
        await store.claim_chat_submission(
            **{**common, "workspace_id": "ws_other"},
        )
    ).kind == "not_found"


@pytest.mark.asyncio
async def test_abandon_wins_over_late_finalize(db_path: str) -> None:
    store = SessionStore(db_path)
    common = {
        "team_id": "team_1",
        "user_id": "u_1",
        "client_submission_id": "018f0cc3-e4a2-7b21-a109-001122334477",
        "workspace_id": "ws_1",
        "requested_session_id": None,
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "request_fingerprint": "d" * 64,
        "accepted_request_fingerprints": ("d" * 64,),
        "fingerprint_key_id": "f" * 64,
        "auth_session_generation_hash": "0" * 64,
    }
    claim = await store.claim_chat_submission(**common)
    assert claim.kind == "claimed"
    abandoned = await store.abandon_chat_submission(
        team_id="team_1",
        user_id="u_1",
        client_submission_id=common["client_submission_id"],
        workspace_id="ws_1",
        requested_session_id=None,
    )
    assert abandoned == "abandoned"
    with pytest.raises(ValueError, match="submission_claim_lost"):
        await store.finalize_started_chat_submission(
            **_finalize_args(common),
            actual_session_id="s_late",
            turn_id="t_late",
            user_text="不能落库",
            attachment_ids=[],
            lease_owner=claim.lease_owner,
            lease_version=claim.lease_version,
            response={
                "session_id": "s_late",
                "turn_id": "t_late",
                "stream_url": "/api/v1/chat/stream/t_late",
                "status": "started",
            },
        )
    assert (await store.claim_chat_submission(**common)).kind == "abandoned"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turns WHERE id='t_late'").fetchone() == (0,)


@pytest.mark.asyncio
async def test_steer_buffer_and_receipt_commit_exactly_once(db_path: str) -> None:
    store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_steer",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_parent",
        session_id="s_steer",
        user_text="parent",
    )
    common = {
        "team_id": "team_1",
        "user_id": "u_1",
        "client_submission_id": "018f0cc3-e4a2-7b21-a109-001122334488",
        "workspace_id": "ws_1",
        "requested_session_id": "s_steer",
        "submission_kind": "steer",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "request_fingerprint": "e" * 64,
        "accepted_request_fingerprints": ("e" * 64,),
        "fingerprint_key_id": "f" * 64,
        "auth_session_generation_hash": "0" * 64,
    }
    claim = await store.claim_chat_submission(**common)
    receipt = await store.finalize_steer_chat_submission(
        team_id="team_1",
        user_id="u_1",
        client_submission_id=common["client_submission_id"],
        workspace_id="ws_1",
        requested_session_id="s_steer",
        expected_parent_turn_id="t_parent",
        expected_execution_generation=1,
        text="取消",
        attachment_ids=[],
        submission_contract_version="chat-submit.v1",
        model_control_batch="a",
        request_fingerprint="e" * 64,
        lease_owner=claim.lease_owner,
        lease_version=claim.lease_version,
    )
    assert receipt.outcome == "buffered"
    replay = await store.claim_chat_submission(**common)
    assert replay.kind == "receipt"
    with sqlite3.connect(db_path) as connection:
        buffer_json = connection.execute("SELECT steer_buffer_json FROM sessions WHERE id='s_steer'").fetchone()[0]
        turn_status = connection.execute("SELECT status FROM turns WHERE id='t_parent'").fetchone()[0]
    assert buffer_json.count("取消") == 1
    assert turn_status == "running"


@pytest.mark.asyncio
async def test_materialize_steer_preserves_reference_boundaries_and_suffix(db_path: str) -> None:
    store = SessionStore(db_path)
    session_id = "s_segmented_steer"
    parent_turn_id = "t_segmented_parent"
    child_turn_id = "t_segmented_plain_child"
    reference_turn_id = "t_segmented_reference_child"
    await store.ensure_session(session_id=session_id, workspace_id="ws_1", user_id="u_1")
    await store.record_turn(turn_id=parent_turn_id, session_id=session_id, user_text="parent")
    assert await store.claim_chat_spawn_dispatch_for_worker(turn_id=parent_turn_id, owner_id="parent-worker") == "claimed"
    buffer = [
        {"text": "plain A", "submission_id": "plain-a"},
        {"text": "plain B", "submission_id": "plain-b"},
        {
            "text": "@Analyst /method reference C",
            "submission_id": "reference-c",
            "reference_submission_id": "reference-c",
        },
        {"text": "plain D", "submission_id": "plain-d"},
    ]
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE sessions SET steer_buffer_json=? WHERE id=?",
            (json.dumps(buffer, ensure_ascii=False), session_id),
        )

    assert await store.materialize_steer_follow_up(
        session_id=session_id,
        current_turn_id=parent_turn_id,
        next_turn_id=child_turn_id,
    ) == ["plain A", "plain B"]
    source = await store.get_chat_turn_user_source(turn_id=child_turn_id, user_id="u_1", workspace_id="ws_1")
    assert source is not None and source.user_text == "plain A\n\nplain B"
    with sqlite3.connect(db_path) as connection:
        session = connection.execute(
            "SELECT active_turn_id,steer_buffer_json FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        dispatch = connection.execute(
            "SELECT source_submission_id,source_submission_ids_json FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (child_turn_id,),
        ).fetchone()
    assert session[0] == child_turn_id and json.loads(session[1]) == buffer[2:]
    assert dispatch == ("plain-a", '["plain-a","plain-b"]')

    assert await store.claim_chat_spawn_dispatch_for_worker(turn_id=child_turn_id, owner_id="child-worker") == "claimed"
    with pytest.raises(ReferenceExecutionUnavailableError):
        await store.materialize_steer_follow_up(
            session_id=session_id,
            current_turn_id=child_turn_id,
            next_turn_id=reference_turn_id,
        )
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT active_turn_id,steer_buffer_json FROM sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            == session
        )
        assert connection.execute("SELECT COUNT(*) FROM turns WHERE id=?", (reference_turn_id,)).fetchone() == (0,)
        assert connection.execute("SELECT status,error_kind FROM turns WHERE id=?", (child_turn_id,)).fetchone() == ("running", None)


@pytest.mark.asyncio
async def test_attachment_only_steer_materializes_canonical_follow_up(db_path: str) -> None:
    store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_attachment_steer",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_attachment_parent",
        session_id="s_attachment_steer",
        user_text="parent",
    )
    common = {
        "team_id": "team_1",
        "user_id": "u_1",
        "client_submission_id": "018f0cc3-e4a2-7b21-a109-001122334489",
        "workspace_id": "ws_1",
        "requested_session_id": "s_attachment_steer",
        "submission_kind": "steer",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "request_fingerprint": "9" * 64,
        "accepted_request_fingerprints": ("9" * 64,),
        "fingerprint_key_id": "f" * 64,
        "auth_session_generation_hash": "0" * 64,
    }
    claim = await store.claim_chat_submission(**common)
    receipt = await store.finalize_steer_chat_submission(
        team_id="team_1",
        user_id="u_1",
        client_submission_id=common["client_submission_id"],
        workspace_id="ws_1",
        requested_session_id="s_attachment_steer",
        expected_parent_turn_id="t_attachment_parent",
        expected_execution_generation=1,
        text="",
        attachment_ids=["art_a", "art_b"],
        attachment_refs=[
            {
                "fileId": "art_bundle",
                "filename": "季度数据.zip",
                "size": 120,
                "mime": "application/zip",
                "bundleArtifactIds": ["art_a", "art_b"],
            }
        ],
        submission_contract_version="chat-submit.v1",
        model_control_batch="a",
        request_fingerprint="9" * 64,
        lease_owner=claim.lease_owner,
        lease_version=claim.lease_version,
    )

    assert receipt.outcome == "buffered"
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id="t_attachment_parent",
            owner_id="gateway-owner",
        )
        == "claimed"
    )
    assert await store.mark_completed(
        turn_id="t_attachment_parent",
        intent="lookup",
        final_answer="parent completed",
        tokens_input=0,
        tokens_output=0,
        cost_usd=0.0,
    )
    await _record_root_done(
        db_path,
        turn_id="t_attachment_parent",
        status="completed",
        final_answer="parent completed",
    )
    assert await store.materialize_steer_follow_up(
        session_id="s_attachment_steer",
        current_turn_id="t_attachment_parent",
        next_turn_id="t_attachment_child",
    ) == [""]
    assert (
        await store.get_chat_spawn_dispatch_projection_state(
            turn_id="t_attachment_parent",
        )
        == "active"
    )
    assert (
        await store.heartbeat_chat_spawn_dispatch(
            turn_id="t_attachment_parent",
            owner_id="gateway-owner",
            settling=True,
        )
        == "renewed"
    )
    source = await store.get_chat_turn_user_source(
        turn_id="t_attachment_child",
        user_id="u_1",
        workspace_id="ws_1",
    )
    assert source is not None
    assert source.user_text == ""
    assert source.attachment_ids == ("art_a", "art_b")
    assert source.attachment_refs == (
        {
            "fileId": "art_bundle",
            "filename": "季度数据.zip",
            "size": 120,
            "mime": "application/zip",
            "bundleArtifactIds": ["art_a", "art_b"],
        },
    )
    assert source.parts == (
        {
            "text": "",
            "attachments": [
                {
                    "fileId": "art_bundle",
                    "filename": "季度数据.zip",
                    "size": 120,
                    "mime": "application/zip",
                    "bundleArtifactIds": ["art_a", "art_b"],
                }
            ],
        },
    )
    assert await store.get_active_turn("s_attachment_steer") == ("t_attachment_child")
    assert await store.append_steer_buffer(
        session_id="s_attachment_steer",
        expected_turn_id="t_attachment_child",
        text="belongs to the child",
    )
    assert await store.mark_chat_spawn_dispatch_terminal(
        turn_id="t_attachment_parent",
        owner_id="gateway-owner",
    )
    assert (
        await store.get_chat_spawn_dispatch_projection_state(
            turn_id="t_attachment_parent",
        )
        == "settled"
    )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id='t_attachment_parent'",
        ).fetchone() == ("terminal",)
        child_buffer = json.loads(
            connection.execute(
                "SELECT steer_buffer_json FROM sessions WHERE id='s_attachment_steer'",
            ).fetchone()[0],
        )
    assert [item["text"] for item in child_buffer] == ["belongs to the child"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dispatch_fence",
    ["missing", "claimed", "expired", "generation_mismatch"],
)
async def test_materialize_steer_requires_active_parent_dispatch_atomically(
    db_path: str,
    dispatch_fence: str,
) -> None:
    store = SessionStore(db_path)
    session_id = f"s_materialize_fence_{dispatch_fence}"
    parent_turn_id = f"t_materialize_fence_{dispatch_fence}"
    child_turn_id = f"t_materialize_fence_{dispatch_fence}_child"
    await store.ensure_session(
        session_id=session_id,
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id=parent_turn_id,
        session_id=session_id,
        user_text="parent",
    )
    await store.append_steer_buffer(
        session_id=session_id,
        text="accepted follow-up",
    )

    now = datetime.now(tz=UTC)
    if dispatch_fence == "claimed":
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                "INSERT INTO chat_turn_spawn_dispatch("
                "turn_id,session_id,source_submission_ids_json,dispatch_kind,state,"
                "lease_kind,lease_execution_generation,owner_id,lease_expires_at,"
                "created_at,updated_at"
                ") VALUES (?,?,'[]','start','claimed','turn',1,NULL,?,?,?)",
                (
                    parent_turn_id,
                    session_id,
                    (now + timedelta(minutes=1)).isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            connection.commit()
    elif dispatch_fence != "missing":
        assert (
            await store.claim_chat_spawn_dispatch_for_worker(
                turn_id=parent_turn_id,
                owner_id="gateway-owner",
            )
            == "claimed"
        )
        with sqlite3.connect(db_path) as connection:
            if dispatch_fence == "expired":
                connection.execute(
                    "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
                    ((now - timedelta(seconds=1)).isoformat(), parent_turn_id),
                )
            else:
                connection.execute(
                    "UPDATE chat_turn_spawn_dispatch SET lease_execution_generation=2 WHERE turn_id=?",
                    (parent_turn_id,),
                )
            connection.commit()

    with sqlite3.connect(db_path) as connection:
        session_before = connection.execute(
            "SELECT active_turn_id,steer_buffer_json FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        dispatch_before = connection.execute(
            "SELECT state,owner_id,lease_expires_at,lease_execution_generation FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (parent_turn_id,),
        ).fetchone()

    with pytest.raises(RuntimeError, match="legacy_parent_dispatch_not_active"):
        await store.materialize_steer_follow_up(
            session_id=session_id,
            current_turn_id=parent_turn_id,
            next_turn_id=child_turn_id,
        )

    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT active_turn_id,steer_buffer_json FROM sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            == session_before
        )
        assert (
            connection.execute(
                "SELECT state,owner_id,lease_expires_at,lease_execution_generation FROM chat_turn_spawn_dispatch WHERE turn_id=?",
                (parent_turn_id,),
            ).fetchone()
            == dispatch_before
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM turns WHERE id=?",
            (child_turn_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (child_turn_id,),
        ).fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed_buffer",
    [
        "not-json",
        "{}",
        '"text"',
        "null",
        '[{"attachment_ids":[123]}]',
        '[{"attachment_refs":["not-an-object"]}]',
        '[{"text":"accepted","submission_id":123}]',
        '[{"text":"accepted","submission_id":"a","reference_submission_id":"b"}]',
    ],
)
async def test_malformed_legacy_continuation_cannot_publish_completed_parent(
    db_path: str,
    malformed_buffer: str,
) -> None:
    store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_malformed_continuation",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_malformed_continuation",
        session_id="s_malformed_continuation",
        user_text="parent",
    )
    now = datetime.now(tz=UTC).isoformat()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE sessions SET steer_buffer_json=? WHERE id='s_malformed_continuation'",
            (malformed_buffer,),
        )
        connection.execute(
            "INSERT INTO chat_turn_spawn_dispatch("
            "turn_id,session_id,source_submission_ids_json,dispatch_kind,state,"
            "owner_id,lease_expires_at,created_at,updated_at"
            ") VALUES (?,?,'[]','start','spawned','legacy-owner',?,?,?)",
            (
                "t_malformed_continuation",
                "s_malformed_continuation",
                now,
                now,
                now,
            ),
        )
        connection.commit()

    assert await store.mark_chat_spawn_dispatch_drain_pending(
        turn_id="t_malformed_continuation",
        owner_id="legacy-owner",
        session_id="s_malformed_continuation",
    )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id='t_malformed_continuation'",
        ).fetchone() == ("drain_pending",)

    with pytest.raises(SessionProtocolManagedError, match="isolated"):
        await store.materialize_steer_follow_up(
            session_id="s_malformed_continuation",
            current_turn_id="t_malformed_continuation",
            next_turn_id="t_malformed_child",
        )
    assert not await store.mark_completed(
        turn_id="t_malformed_continuation",
        intent=None,
        final_answer="must not win",
        tokens_input=0,
        tokens_output=0,
        cost_usd=0.0,
    )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT status,error_kind FROM turns WHERE id='t_malformed_continuation'",
        ).fetchone() == ("failed", "legacy_protocol_isolated")
        assert connection.execute(
            "SELECT active_turn_id,steer_buffer_json FROM sessions WHERE id='s_malformed_continuation'",
        ).fetchone() == (None, "[]")
        assert connection.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id='t_malformed_continuation'",
        ).fetchone() == ("failed",)
        assert connection.execute(
            "SELECT COUNT(*) FROM turns WHERE id='t_malformed_child'",
        ).fetchone() == (0,)

    # The isolated opaque payload cannot leak into the next valid parent.
    await store.record_turn(
        turn_id="t_after_malformed_continuation",
        session_id="s_malformed_continuation",
        user_text="fresh parent",
    )
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id="t_after_malformed_continuation",
            owner_id="owner-fresh",
        )
        == "claimed"
    )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT active_turn_id,steer_buffer_json FROM sessions WHERE id='s_malformed_continuation'",
        ).fetchone() == ("t_after_malformed_continuation", "[]")


@pytest.mark.asyncio
async def test_expired_receipt_is_not_replayed_and_cleanup_is_bounded(db_path: str) -> None:
    store = SessionStore(db_path)
    expired = (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat()
    future = (datetime.now(tz=UTC) + timedelta(days=1)).isoformat()
    base = (
        "team_1",
        "u_1",
        "018f0cc3-e4a2-7b21-a109-001122334499",
        "ws_1",
        None,
        "start",
        "chat-submit.v1",
        "a",
        "f" * 64,
        "e" * 64,
        "0" * 64,
        "started",
        '{"status":"started"}',
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO chat_submission_receipts("
            "team_id,user_id,client_submission_id,workspace_id,requested_session_id,"
            "submission_kind,submission_contract_version,model_control_batch,"
            "request_fingerprint,fingerprint_key_id,auth_session_generation_hash,"
            "outcome,response_json,created_at,expires_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (*base, expired, expired),
        )
        for suffix, expires_at in (("500", expired), ("501", future)):
            connection.execute(
                "INSERT INTO chat_submission_abandon_tombstones("
                "team_id,user_id,client_submission_id,workspace_id,requested_session_id,"
                "created_at,expires_at) VALUES (?,?,?,?,?,?,?)",
                ("team_1", "u_1", f"018f0cc3-e4a2-7b21-a109-001122334{suffix}", "ws_1", None, expired, expires_at),
            )
        connection.commit()

    assert (
        await store.get_chat_submission_receipt(
            team_id="team_1",
            user_id="u_1",
            client_submission_id=base[2],
            workspace_id="ws_1",
            requested_session_id=None,
        )
        is None
    )
    claim = await store.claim_chat_submission(
        team_id="team_1",
        user_id="u_1",
        client_submission_id=base[2],
        workspace_id="ws_1",
        requested_session_id=None,
        submission_kind="start",
        submission_contract_version="chat-submit.v1",
        model_control_batch="a",
        request_fingerprint="f" * 64,
        accepted_request_fingerprints=("f" * 64,),
        fingerprint_key_id="e" * 64,
        auth_session_generation_hash="0" * 64,
    )
    assert claim.kind == "claimed"
    assert await store.cleanup_chat_submission_protocol(batch_size=1) == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM chat_submission_abandon_tombstones").fetchone() == (1,)


@pytest.mark.asyncio
async def test_typed_start_cannot_replace_an_active_session_turn(db_path: str) -> None:
    store = SessionStore(db_path)
    await store.ensure_session(session_id="s_active", workspace_id="ws_1", user_id="u_1")
    await store.record_turn(turn_id="t_parent", session_id="s_active", user_text="parent")
    common = {
        "team_id": "team_1",
        "user_id": "u_1",
        "client_submission_id": "018f0cc3-e4a2-7b21-a109-001122334510",
        "workspace_id": "ws_1",
        "requested_session_id": "s_active",
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "request_fingerprint": "1" * 64,
        "accepted_request_fingerprints": ("1" * 64,),
        "fingerprint_key_id": "f" * 64,
        "auth_session_generation_hash": "0" * 64,
    }
    claim = await store.claim_chat_submission(**common)

    with pytest.raises(ValueError, match="submission_session_active"):
        await store.finalize_started_chat_submission(
            **_finalize_args(common),
            actual_session_id="s_active",
            turn_id="t_forbidden",
            user_text="must not replace",
            attachment_ids=[],
            lease_owner=claim.lease_owner,
            lease_version=claim.lease_version,
            response={"status": "started"},
        )

    assert await store.get_active_turn("s_active") == "t_parent"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turns WHERE id='t_forbidden'").fetchone() == (0,)


@pytest.mark.asyncio
async def test_home_start_cannot_replace_an_active_session_turn(db_path: str) -> None:
    store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_home_active",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_home_parent",
        session_id="s_home_active",
        user_text="parent",
    )
    common = {
        "team_id": "team_1",
        "user_id": "u_1",
        "client_submission_id": "018f0cc3-e4a2-7b21-a109-001122334520",
        "workspace_id": "ws_1",
        "requested_session_id": None,
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "request_fingerprint": "3" * 64,
        "accepted_request_fingerprints": ("3" * 64,),
        "fingerprint_key_id": "f" * 64,
        "auth_session_generation_hash": "0" * 64,
    }
    claim = await store.claim_chat_submission(**common)

    with pytest.raises(ValueError, match="submission_session_active"):
        await store.finalize_started_chat_submission(
            **_finalize_args(common),
            actual_session_id="s_home_active",
            turn_id="t_home_forbidden",
            user_text="must not replace",
            attachment_ids=[],
            lease_owner=claim.lease_owner,
            lease_version=claim.lease_version,
            response={"status": "started"},
        )

    assert await store.get_active_turn("s_home_active") == "t_home_parent"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM turns WHERE id='t_home_forbidden'",
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_record_turn_replays_exact_turn_but_rejects_new_active_turn(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_record_active",
        workspace_id="ws_1",
        user_id="u_1",
    )
    first = await store.record_turn(
        turn_id="t_record_first",
        session_id="s_record_active",
        user_text="first",
    )

    replay = await store.record_turn(
        turn_id="t_record_first",
        session_id="s_record_active",
        user_text="first",
    )
    assert replay == first

    with pytest.raises(ValueError, match="submission_session_active"):
        await store.record_turn(
            turn_id="t_record_second",
            session_id="s_record_active",
            user_text="second",
        )

    assert await store.get_active_turn("s_record_active") == "t_record_first"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM turns WHERE session_id='s_record_active'",
        ).fetchone() == (1,)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["completed", "failed", "cancelled"])
async def test_record_turn_never_replays_a_terminal_legacy_turn(
    db_path: str,
    terminal_status: str,
) -> None:
    store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_record_terminal",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_record_terminal",
        session_id="s_record_terminal",
        user_text="same request",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turns SET status=?,ended_at=? WHERE id=?",
            (terminal_status, datetime.now(tz=UTC).isoformat(), "t_record_terminal"),
        )
        connection.execute(
            "UPDATE sessions SET active_turn_id=NULL WHERE id=?",
            ("s_record_terminal",),
        )
        connection.commit()

    with pytest.raises(ValueError, match="submission_turn_conflict"):
        await store.record_turn(
            turn_id="t_record_terminal",
            session_id="s_record_terminal",
            user_text="same request",
        )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT status FROM turns WHERE id='t_record_terminal'",
        ).fetchone() == (terminal_status,)
        assert connection.execute(
            "SELECT active_turn_id FROM sessions WHERE id='s_record_terminal'",
        ).fetchone() == (None,)


@pytest.mark.asyncio
async def test_record_turn_never_replays_an_idle_running_legacy_turn(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_record_idle",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_record_idle",
        session_id="s_record_idle",
        user_text="same request",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE sessions SET active_turn_id=NULL WHERE id='s_record_idle'",
        )
        connection.commit()

    with pytest.raises(ValueError, match="submission_turn_conflict"):
        await store.record_turn(
            turn_id="t_record_idle",
            session_id="s_record_idle",
            user_text="same request",
        )


@pytest.mark.asyncio
async def test_submission_finalizer_never_adopts_an_existing_legacy_turn(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_existing_turn",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_existing_turn",
        session_id="s_existing_turn",
        user_text="same text",
    )
    common = {
        "team_id": "team_1",
        "user_id": "u_1",
        "client_submission_id": "018f0cc3-e4a2-7b21-a109-001122334521",
        "workspace_id": "ws_1",
        "requested_session_id": None,
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "request_fingerprint": "4" * 64,
        "accepted_request_fingerprints": ("4" * 64,),
        "fingerprint_key_id": "f" * 64,
        "auth_session_generation_hash": "0" * 64,
    }
    claim = await store.claim_chat_submission(**common)

    with pytest.raises(ValueError, match="submission_turn_conflict"):
        await store.finalize_started_chat_submission(
            **_finalize_args(common),
            actual_session_id="s_existing_turn",
            turn_id="t_existing_turn",
            user_text="same text",
            attachment_ids=[],
            lease_owner=claim.lease_owner,
            lease_version=claim.lease_version,
            response={"status": "started"},
        )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_submission_receipts WHERE client_submission_id=?",
            (common["client_submission_id"],),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_turn_spawn_dispatch WHERE turn_id='t_existing_turn'",
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_turn_user_sources WHERE turn_id='t_existing_turn'",
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT active_turn_id FROM sessions WHERE id='s_existing_turn'",
        ).fetchone() == ("t_existing_turn",)


@pytest.mark.asyncio
async def test_stale_steer_generation_is_a_durable_rejection(db_path: str) -> None:
    store = SessionStore(db_path)
    await store.ensure_session(session_id="s_gen", workspace_id="ws_1", user_id="u_1")
    await store.record_turn(turn_id="t_gen", session_id="s_gen", user_text="parent")
    common = {
        "team_id": "team_1",
        "user_id": "u_1",
        "client_submission_id": "018f0cc3-e4a2-7b21-a109-001122334511",
        "workspace_id": "ws_1",
        "requested_session_id": "s_gen",
        "submission_kind": "steer",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "request_fingerprint": "2" * 64,
        "accepted_request_fingerprints": ("2" * 64,),
        "fingerprint_key_id": "f" * 64,
        "auth_session_generation_hash": "0" * 64,
    }
    claim = await store.claim_chat_submission(**common)
    receipt = await store.finalize_steer_chat_submission(
        team_id="team_1",
        user_id="u_1",
        client_submission_id=common["client_submission_id"],
        workspace_id="ws_1",
        requested_session_id="s_gen",
        expected_parent_turn_id="t_gen",
        expected_execution_generation=2,
        text="stale",
        attachment_ids=[],
        submission_contract_version="chat-submit.v1",
        model_control_batch="a",
        request_fingerprint="2" * 64,
        lease_owner=claim.lease_owner,
        lease_version=claim.lease_version,
    )

    assert receipt.outcome == "rejected"
    assert receipt.turn_id is None
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT steer_buffer_json FROM sessions WHERE id='s_gen'").fetchone() == ("[]",)


@pytest.mark.asyncio
async def test_resume_generation_advances_once_and_fences_old_steer(db_path: str) -> None:
    store = SessionStore(db_path)
    await store.ensure_session(session_id="s_resume_gen", workspace_id="ws_1", user_id="u_1")
    await store.record_turn(turn_id="t_resume_gen", session_id="s_resume_gen", user_text="parent")
    await store.mark_clarification_pending(
        turn_id="t_resume_gen",
        intent="clarify",
    )

    assert await store.advance_turn_execution_generation(turn_id="t_resume_gen") == 2
    assert await store.advance_turn_execution_generation(turn_id="t_resume_gen") is None
    turn = await store.get_turn("t_resume_gen")
    assert turn is not None
    assert turn.status == "running"
    assert turn.execution_generation == 2

    common = {
        "team_id": "team_1",
        "user_id": "u_1",
        "workspace_id": "ws_1",
        "requested_session_id": "s_resume_gen",
        "submission_kind": "steer",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "request_fingerprint": "4" * 64,
        "accepted_request_fingerprints": ("4" * 64,),
        "fingerprint_key_id": "f" * 64,
        "auth_session_generation_hash": "0" * 64,
    }
    stale_id = "018f0cc3-e4a2-7b21-a109-001122334521"
    stale_claim = await store.claim_chat_submission(
        **common,
        client_submission_id=stale_id,
    )
    finalize_common = {k: v for k, v in _finalize_args(common).items() if k != "submission_kind"}
    stale = await store.finalize_steer_chat_submission(
        **finalize_common,
        client_submission_id=stale_id,
        expected_parent_turn_id="t_resume_gen",
        expected_execution_generation=1,
        text="stale",
        attachment_ids=[],
        lease_owner=stale_claim.lease_owner,
        lease_version=stale_claim.lease_version,
    )
    assert stale.outcome == "rejected"

    current_id = "018f0cc3-e4a2-7b21-a109-001122334522"
    current_claim = await store.claim_chat_submission(
        **{
            **common,
            "request_fingerprint": "5" * 64,
            "accepted_request_fingerprints": ("5" * 64,),
        },
        client_submission_id=current_id,
    )
    current = await store.finalize_steer_chat_submission(
        **{**finalize_common, "request_fingerprint": "5" * 64},
        client_submission_id=current_id,
        expected_parent_turn_id="t_resume_gen",
        expected_execution_generation=2,
        text="current",
        attachment_ids=[],
        lease_owner=current_claim.lease_owner,
        lease_version=current_claim.lease_version,
    )
    assert current.outcome == "buffered"


@pytest.mark.asyncio
async def test_resume_generation_waits_for_prior_dispatch_settlement(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_resume_settle",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_resume_settle",
        session_id="s_resume_settle",
        user_text="parent",
    )
    assert await store.mark_clarification_pending(
        turn_id="t_resume_settle",
        intent="clarify",
    )
    now = datetime.now(tz=UTC).isoformat()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO chat_turn_spawn_dispatch("
            "turn_id,session_id,source_submission_ids_json,dispatch_kind,state,"
            "lease_kind,lease_execution_generation,owner_id,lease_expires_at,"
            "created_at,updated_at) VALUES ("
            "?,?,'[]','start','spawned','turn',1,?,?,?,?)",
            (
                "t_resume_settle",
                "s_resume_settle",
                "old-worker",
                (datetime.now(tz=UTC) + timedelta(seconds=30)).isoformat(),
                now,
                now,
            ),
        )
        connection.commit()

    assert await store.advance_turn_execution_generation(turn_id="t_resume_settle") is None
    waiting = await store.get_turn("t_resume_settle")
    assert waiting is not None
    assert waiting.status == "clarification_pending"
    assert waiting.execution_generation == 1

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET state='terminal' WHERE turn_id=?",
            ("t_resume_settle",),
        )
        connection.commit()
    assert await store.advance_turn_execution_generation(turn_id="t_resume_settle") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted_steer", [False, True])
async def test_settled_idle_clarification_resume_claim_and_rollback_are_retryable(
    db_path: str,
    accepted_steer: bool,
) -> None:
    store = SessionStore(db_path)
    session_id = "s_idle_clarification_resume"
    turn_id = "t_idle_clarification_resume"
    await store.ensure_session(
        session_id=session_id,
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id=turn_id,
        session_id=session_id,
        user_text="parent",
    )
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id=turn_id,
            owner_id="pause-owner",
        )
        == "claimed"
    )
    assert await store.mark_clarification_pending(
        turn_id=turn_id,
        intent="clarify",
    )

    # The terminal hook clears an empty parent pointer before the central
    # monitor settles its exact owner. This is the real pause lifecycle.
    assert (
        await store.materialize_steer_follow_up(
            session_id=session_id,
            current_turn_id=turn_id,
            next_turn_id="unused_child",
        )
        == []
    )
    assert await store.get_active_turn(session_id) is None
    await _record_root_done(
        db_path,
        turn_id=turn_id,
        status="clarification_pending",
    )
    assert await store.mark_chat_spawn_dispatch_terminal(
        turn_id=turn_id,
        owner_id="pause-owner",
    )
    assert await store.is_turn_resume_ready(turn_id)

    assert await store.advance_turn_execution_generation(turn_id=turn_id) == 2
    assert await store.get_active_turn(session_id) == turn_id
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id=turn_id,
            owner_id="resume-owner",
        )
        == "claimed"
    )
    if accepted_steer:
        assert await store.append_steer_buffer(
            session_id=session_id,
            text="concurrent accepted steer",
            expected_turn_id=turn_id,
        )
    assert await store.rollback_turn_resume_generation(
        turn_id=turn_id,
        expected_execution_generation=2,
        owner_id="resume-owner",
    )
    assert await store.get_active_turn(session_id) == (turn_id if accepted_steer else None)
    with sqlite3.connect(db_path) as connection:
        buffered = json.loads(
            connection.execute(
                "SELECT steer_buffer_json FROM sessions WHERE id=?",
                (session_id,),
            ).fetchone()[0],
        )
    assert [item["text"] for item in buffered] == (["concurrent accepted steer"] if accepted_steer else [])
    assert await store.is_turn_resume_ready(turn_id)
    assert await store.advance_turn_execution_generation(turn_id=turn_id) == 3
    assert await store.get_active_turn(session_id) == turn_id


@pytest.mark.asyncio
async def test_resume_rollback_repair_continues_after_legacy_row_cursor(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    log_store = TurnLogStore(db_path)
    await log_store.initialize()
    session_id = "s_resume_legacy_cursor"
    turn_id = "t_resume_legacy_cursor"
    await store.ensure_session(
        session_id=session_id,
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id=turn_id,
        session_id=session_id,
        user_text="resume cursor",
    )
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id=turn_id,
            owner_id="pause-owner",
        )
        == "claimed"
    )
    assert await store.mark_clarification_pending(
        turn_id=turn_id,
        intent="clarify",
    )
    await _record_root_done(
        db_path,
        turn_id=turn_id,
        status="clarification_pending",
    )
    assert await store.mark_chat_spawn_dispatch_terminal(
        turn_id=turn_id,
        owner_id="pause-owner",
    )
    assert await store.advance_turn_execution_generation(turn_id=turn_id) == 2
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id=turn_id,
            owner_id="resume-owner",
        )
        == "claimed"
    )
    occurred_at = datetime.now(tz=UTC).isoformat()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES (?,?,2,'turn.resumed',?,?)",
            (
                "evt_resume_legacy_cursor",
                turn_id,
                json.dumps(
                    {
                        "turn_id": turn_id,
                        "execution_generation": 2,
                        "seq": 2,
                    }
                ),
                occurred_at,
            ),
        )
        connection.execute(
            "INSERT INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES (?,?,3,'narrative.delta',?,?)",
            (
                "evt_resume_legacy_narrative",
                turn_id,
                json.dumps({"seq": 3, "text": "legacy"}),
                occurred_at,
            ),
        )
        connection.commit()

    assert await store.rollback_turn_resume_generation(
        turn_id=turn_id,
        expected_execution_generation=2,
        owner_id="resume-owner",
    )
    events = await log_store.list_events(turn_id)
    assert events[-1].seq == 4
    assert events[-1].event_type == "done"
    assert events[-1].payload["seq"] == 4
    assert events[-1].payload["evt_seq"] == 4


@pytest.mark.asyncio
async def test_remote_resume_attach_requires_live_spawned_lease(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_remote_resume",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_remote_resume",
        session_id="s_remote_resume",
        user_text="parent",
    )
    assert await store.mark_clarification_pending(
        turn_id="t_remote_resume",
        intent="clarify",
    )
    assert (
        await store.advance_turn_execution_generation(
            turn_id="t_remote_resume",
        )
        == 2
    )

    # Generation CAS alone is not attach proof: the worker is not scheduled.
    assert (
        await store.get_active_resume_dispatch_generation(
            turn_id="t_remote_resume",
        )
        is None
    )
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id="t_remote_resume",
            owner_id="gateway-b",
        )
        == "claimed"
    )
    assert (
        await store.get_active_resume_dispatch_generation(
            turn_id="t_remote_resume",
        )
        == 2
    )
    assert await store.cleanup_stale_running_turns(older_than_min=0) == 0

    with sqlite3.connect(db_path) as connection:
        now = datetime.now(tz=UTC).isoformat()
        connection.execute(
            "INSERT INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES (?,?,?,?,?,?)",
            (
                "evt_resume_old_paused_done",
                "t_remote_resume",
                1,
                "done",
                '{"status":"clarification_pending","evt_seq":1}',
                now,
            ),
        )
        connection.execute(
            "INSERT INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES (?,?,?,?,?,?)",
            (
                "evt_resume_generation_2",
                "t_remote_resume",
                2,
                "turn.resumed",
                '{"execution_generation":2,"evt_seq":2}',
                now,
            ),
        )
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
            (
                (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat(),
                "t_remote_resume",
            ),
        )
        connection.commit()
    assert (
        await store.get_active_resume_dispatch_generation(
            turn_id="t_remote_resume",
        )
        is None
    )
    assert await store.recover_expired_chat_spawn_dispatches() == ["t_remote_resume"]
    failed = await store.get_turn("t_remote_resume")
    assert failed is not None
    assert failed.status == "failed"
    assert failed.error_kind == "dispatch_owner_lost"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT event_type FROM turn_events WHERE turn_id=? AND seq>2 ORDER BY seq",
            ("t_remote_resume",),
        ).fetchall() == [("turn_failed",), ("done",)]


@pytest.mark.asyncio
async def test_expired_spawned_resume_generation_mismatch_is_isolated(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    session_id = "s_resume_generation_mismatch"
    turn_id = "t_resume_generation_mismatch"
    await store.ensure_session(
        session_id=session_id,
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id=turn_id,
        session_id=session_id,
        user_text="parent",
    )
    assert await store.mark_clarification_pending(
        turn_id=turn_id,
        intent="clarify",
    )
    assert await store.advance_turn_execution_generation(turn_id=turn_id) == 2
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id=turn_id,
            owner_id="resume-owner",
        )
        == "claimed"
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_execution_generation=1,lease_expires_at=? WHERE turn_id=?",
            (
                (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat(),
                turn_id,
            ),
        )
        connection.commit()

    assert await store.recover_expired_chat_spawn_dispatches() == [turn_id]
    isolated = await store.get_turn(turn_id)
    assert isolated is not None
    assert isolated.status == "failed"
    assert isolated.error_kind == "legacy_protocol_isolated"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state,lease_kind,lease_execution_generation FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == ("failed", "resume", 1)
    assert await store.get_active_turn(session_id) == turn_id


@pytest.mark.asyncio
async def test_fresh_start_failure_handoff_never_claims_resume_lease(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_resume_handoff_fence",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_resume_handoff_fence",
        session_id="s_resume_handoff_fence",
        user_text="parent",
    )
    assert await store.mark_clarification_pending(
        turn_id="t_resume_handoff_fence",
        intent="clarify",
    )
    assert (
        await store.advance_turn_execution_generation(
            turn_id="t_resume_handoff_fence",
        )
        == 2
    )
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id="t_resume_handoff_fence",
            owner_id="resume-owner",
        )
        == "claimed"
    )

    assert (
        await store.prepare_chat_spawn_failure(
            turn_id="t_resume_handoff_fence",
            session_id="s_resume_handoff_fence",
            expected_owner_id="resume-owner",
        )
        == "foreign_active"
    )
    turn = await store.get_turn("t_resume_handoff_fence")
    assert turn is not None and turn.status == "running"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state,lease_kind,owner_id FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            ("t_resume_handoff_fence",),
        ).fetchone() == ("spawned", "resume", "resume-owner")


@pytest.mark.asyncio
async def test_resume_recovery_ignores_prior_generation_done(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    log_store = TurnLogStore(db_path)
    await log_store.initialize()
    await store.ensure_session(
        session_id="s_resume_missing_done",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_resume_missing_done",
        session_id="s_resume_missing_done",
        user_text="parent",
    )
    assert await store.mark_clarification_pending(
        turn_id="t_resume_missing_done",
        intent="clarify",
        error_kind="needs_clarification",
    )
    assert (
        await store.advance_turn_execution_generation(
            turn_id="t_resume_missing_done",
        )
        == 2
    )
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id="t_resume_missing_done",
            owner_id="resume-owner",
        )
        == "claimed"
    )
    assert await store.mark_completed(
        turn_id="t_resume_missing_done",
        intent="lookup",
        final_answer="generation two answer",
        tokens_input=13,
        tokens_output=17,
        cost_usd=0.5,
    )
    now = datetime.now(tz=UTC).isoformat()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES (?,?,?,?,?,?)",
            (
                "evt_resume_missing_done_old_answer",
                "t_resume_missing_done",
                1,
                "final_answer",
                '{"text":"generation one answer","evt_seq":1}',
                now,
            ),
        )
        connection.execute(
            "INSERT INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES (?,?,?,?,?,?)",
            (
                "evt_resume_missing_done_old",
                "t_resume_missing_done",
                2,
                "done",
                '{"status":"clarification_pending","evt_seq":2}',
                now,
            ),
        )
        connection.execute(
            "INSERT INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES (?,?,?,?,?,?)",
            (
                "evt_resume_missing_done_generation",
                "t_resume_missing_done",
                3,
                "turn.resumed",
                '{"execution_generation":2,"evt_seq":3}',
                now,
            ),
        )
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
            (
                (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat(),
                "t_resume_missing_done",
            ),
        )
        connection.commit()
    before = await store.get_turn("t_resume_missing_done")

    assert await store.recover_expired_chat_spawn_dispatches() == [
        "t_resume_missing_done",
    ]
    assert await store.get_turn("t_resume_missing_done") == before
    events = await log_store.list_events("t_resume_missing_done")
    assert [event.event_type for event in events] == [
        "final_answer",
        "done",
        "turn.resumed",
        "final_answer",
        "done",
    ]
    assert events[-2].payload["text"] == "generation two answer"
    assert events[-1].payload["status"] == "completed"
    assert events[-1].seq > 3


@pytest.mark.asyncio
async def test_legacy_running_turn_without_dispatch_is_atomically_claimed(
    db_path: str,
) -> None:
    """A pure-legacy Turn gets one durable worker/Stop fence before execution."""

    store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_legacy_worker_claim",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_legacy_worker_claim",
        session_id="s_legacy_worker_claim",
        user_text="legacy channel start",
    )

    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id="t_legacy_worker_claim",
            owner_id="gateway-a",
        )
        == "claimed"
    )
    assert await store.get_active_turn("s_legacy_worker_claim") == "t_legacy_worker_claim"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state,lease_kind,lease_execution_generation,owner_id FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            ("t_legacy_worker_claim",),
        ).fetchone() == ("spawned", "turn", 1, "gateway-a")


@pytest.mark.asyncio
async def test_clarification_without_dispatch_waits_for_settlement_bootstrap(
    db_path: str,
) -> None:
    """A missing legacy row is uncertainty, never proof that resume is safe."""

    store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_legacy_clarification",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_legacy_clarification",
        session_id="s_legacy_clarification",
        user_text="legacy clarification",
    )
    assert await store.mark_clarification_pending(
        turn_id="t_legacy_clarification",
        intent="clarify",
    )
    assert not await store.is_turn_resume_ready("t_legacy_clarification")

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turns SET ended_at=? WHERE id=?",
            (
                (datetime.now(tz=UTC) - timedelta(minutes=2)).isoformat(),
                "t_legacy_clarification",
            ),
        )
        connection.commit()
    assert await store.recover_expired_chat_spawn_dispatches() == [
        "t_legacy_clarification",
    ]
    assert await store.is_turn_resume_ready("t_legacy_clarification")
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            ("t_legacy_clarification",),
        ).fetchone() == ("failed",)
        done = connection.execute(
            "SELECT payload_json FROM turn_events WHERE turn_id=? AND event_type='done' ORDER BY seq DESC LIMIT 1",
            ("t_legacy_clarification",),
        ).fetchone()
    assert done is not None
    assert json.loads(done[0])["status"] == "clarification_pending"
    request_id = "req_legacy_clarification"
    payload = {
        "request_id": request_id,
        "chosen_value": "recovered choice",
    }
    _record_completed_clarification_decision(
        db_path,
        turn_id="t_legacy_clarification",
        request_id=request_id,
        payload=payload,
    )
    resolved = await TurnLogStore(db_path).record(
        turn_id="t_legacy_clarification",
        event_type="clarification.resolved",
        payload=payload,
        event_id=f"evt_clarification_resolution_{request_id}",
    )
    assert resolved is not None


@pytest.mark.asyncio
async def test_idle_historical_clarification_bootstraps_settled_resume_fence(
    db_path: str,
) -> None:
    """An old paused Turn stays closed until maintenance proves its idle tail."""

    store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_idle_legacy_clarification",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_idle_legacy_clarification",
        session_id="s_idle_legacy_clarification",
        user_text="legacy clarification",
    )
    assert await store.mark_clarification_pending(
        turn_id="t_idle_legacy_clarification",
        intent="clarify",
    )
    await store.finalize_turn(
        session_id="s_idle_legacy_clarification",
        current_turn_id="t_idle_legacy_clarification",
        next_turn_id=None,
    )
    assert not await store.is_turn_resume_ready(
        "t_idle_legacy_clarification",
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turns SET ended_at=? WHERE id=?",
            (
                (datetime.now(tz=UTC) - timedelta(minutes=2)).isoformat(),
                "t_idle_legacy_clarification",
            ),
        )
        connection.commit()

    assert await store.recover_expired_chat_spawn_dispatches() == [
        "t_idle_legacy_clarification",
    ]
    assert await store.is_turn_resume_ready("t_idle_legacy_clarification")
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            ("t_idle_legacy_clarification",),
        ).fetchone() == ("failed",)
        done = connection.execute(
            "SELECT payload_json FROM turn_events WHERE turn_id=? AND event_type='done' ORDER BY seq DESC LIMIT 1",
            ("t_idle_legacy_clarification",),
        ).fetchone()
    assert done is not None
    assert json.loads(done[0])["status"] == "clarification_pending"
    request_id = "req_idle_legacy_clarification"
    payload = {
        "request_id": request_id,
        "chosen_value": "recovered choice",
    }
    _record_completed_clarification_decision(
        db_path,
        turn_id="t_idle_legacy_clarification",
        request_id=request_id,
        payload=payload,
    )
    resolved = await TurnLogStore(db_path).record(
        turn_id="t_idle_legacy_clarification",
        event_type="clarification.resolved",
        payload=payload,
        event_id=f"evt_clarification_resolution_{request_id}",
    )
    assert resolved is not None


@pytest.mark.asyncio
async def test_failed_dispatch_accepts_only_canonical_clarification_receipt(
    db_path: str,
) -> None:
    """A crashed pause keeps its stale-writer fence without losing a choice."""

    store = SessionStore(db_path)
    log_store = TurnLogStore(db_path)
    session_id = "s_failed_clarification_receipt"
    turn_id = "t_failed_clarification_receipt"
    request_id = "req_failed_clarification_receipt"
    payload = {
        "request_id": request_id,
        "chosen_value": "canonical choice",
    }
    await store.ensure_session(
        session_id=session_id,
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id=turn_id,
        session_id=session_id,
        user_text="choose one",
    )
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id=turn_id,
            owner_id="owner-lost",
        )
        == "claimed"
    )
    assert await store.mark_clarification_pending(
        turn_id=turn_id,
        intent="clarify",
    )
    await _record_root_done(
        db_path,
        turn_id=turn_id,
        status="clarification_pending",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
            (
                (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat(),
                turn_id,
            ),
        )
        connection.commit()

    assert await store.recover_expired_chat_spawn_dispatches() == [turn_id]
    assert await store.is_turn_resume_ready(turn_id)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == ("failed",)

    _record_completed_clarification_decision(
        db_path,
        turn_id=turn_id,
        request_id=request_id,
        payload=payload,
    )
    event_id = f"evt_clarification_resolution_{request_id}"
    assert (
        await log_store.record(
            turn_id=turn_id,
            event_type="clarification.resolved",
            payload=payload,
            event_id="evt_clarification_resolution_forged",
        )
        is None
    )
    assert (
        await log_store.record(
            turn_id=turn_id,
            event_type="clarification.resolved",
            payload={**payload, "chosen_value": "forged choice"},
            event_id=event_id,
        )
        is None
    )
    assert (
        await log_store.record(
            turn_id=turn_id,
            event_type="partial_answer",
            payload={"text": "stale owner"},
        )
        is None
    )

    inserted = await log_store.record(
        turn_id=turn_id,
        event_type="clarification.resolved",
        payload=payload,
        event_id=event_id,
    )
    assert inserted is not None and inserted.inserted
    duplicate = await log_store.record(
        turn_id=turn_id,
        event_type="clarification.resolved",
        payload={**payload, "seq": 99, "evt_seq": 99},
        event_id=event_id,
    )
    assert duplicate is not None and not duplicate.inserted
    assert await store.advance_turn_execution_generation(turn_id=turn_id) == 2
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state,lease_kind,lease_execution_generation FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == ("claimed", "resume", 2)


@pytest.mark.parametrize("terminal_status", ["failed", "cancelled", "aborted"])
@pytest.mark.asyncio
async def test_shared_im_resume_generation_allows_failure_terminals(
    db_path: str,
    terminal_status: str,
) -> None:
    """Feishu uses the shared resume fence before any terminal path."""

    store = SessionStore(db_path)
    session_id = f"s_im_resume_{terminal_status}"
    turn_id = f"t_im_resume_{terminal_status}"
    await store.ensure_session(
        session_id=session_id,
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(turn_id=turn_id, session_id=session_id, user_text="parent")
    assert await store.mark_clarification_pending(turn_id=turn_id, intent="clarify")
    assert await store.advance_turn_execution_generation(turn_id=turn_id) == 2

    if terminal_status == "failed":
        transitioned = await store.mark_failed(
            turn_id=turn_id,
            error="resume_failed",
            error_kind="internal",
        )
    elif terminal_status == "cancelled":
        transitioned = await store.mark_cancelled(turn_id=turn_id)
    else:
        transitioned = await store.mark_aborted(turn_id=turn_id)

    assert transitioned is True
    turn = await store.get_turn(turn_id)
    assert turn is not None
    assert turn.status == terminal_status
    assert turn.execution_generation == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted_steer", [False, True])
async def test_resume_preworker_lease_survives_cleanup_then_recovers_retryably(
    db_path: str,
    accepted_steer: bool,
) -> None:
    """Old clarification age cannot kill a live resume; expired pre-worker work retries."""

    store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_resume_orphan",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_resume_orphan",
        session_id="s_resume_orphan",
        user_text="parent",
    )
    assert await store.mark_clarification_pending(
        turn_id="t_resume_orphan",
        intent="clarify",
    )
    assert (
        await store.advance_turn_execution_generation(
            turn_id="t_resume_orphan",
        )
        == 2
    )
    if accepted_steer:
        assert await store.append_steer_buffer(
            session_id="s_resume_orphan",
            text="accepted while resume was preworker",
            expected_turn_id="t_resume_orphan",
        )
    stale_started = (datetime.now(tz=UTC) - timedelta(minutes=11)).isoformat()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turns SET started_at=? WHERE id='t_resume_orphan'",
            (stale_started,),
        )
        connection.commit()

    assert await store.cleanup_stale_running_turns(older_than_min=10) == 0
    assert await store.cleanup_stale_running_turns(older_than_min=0) == 0
    turn = await store.get_turn("t_resume_orphan")
    assert turn is not None
    assert turn.status == "running"

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id='t_resume_orphan'",
            ((datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat(),),
        )
        connection.commit()

    # Maintenance takes a fresh clock snapshot after recovery. Even if the
    # claimed lease crosses expiry in that gap, compatibility cleanup must not
    # steal tracked resume semantics.
    assert await store.cleanup_stale_running_turns(older_than_min=0) == 0
    still_running = await store.get_turn("t_resume_orphan")
    assert still_running is not None and still_running.status == "running"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state,lease_kind FROM chat_turn_spawn_dispatch WHERE turn_id='t_resume_orphan'",
        ).fetchone() == ("claimed", "resume")

    assert await store.recover_expired_chat_spawn_dispatches() == ["t_resume_orphan"]
    retryable = await store.get_turn("t_resume_orphan")
    assert retryable is not None
    assert retryable.status == "clarification_pending"
    assert await store.get_active_turn("s_resume_orphan") == ("t_resume_orphan" if accepted_steer else None)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id='t_resume_orphan'",
        ).fetchone() == ("terminal",)
        buffered = json.loads(
            connection.execute(
                "SELECT steer_buffer_json FROM sessions WHERE id='s_resume_orphan'",
            ).fetchone()[0],
        )
    assert [item["text"] for item in buffered] == (["accepted while resume was preworker"] if accepted_steer else [])
    assert (
        await store.advance_turn_execution_generation(
            turn_id="t_resume_orphan",
        )
        == 3
    )


@pytest.mark.asyncio
async def test_composer_telemetry_uses_server_time_dedupe_and_retention(db_path: str) -> None:
    store = SessionStore(db_path)
    received = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    event_id = "018f0cc3-e4a2-7b21-a109-001122334531"

    assert await store.record_composer_telemetry_event(
        user_id="u_1",
        event_id=event_id,
        occurred_at=received + timedelta(minutes=10),
        received_at=received,
    )
    assert not await store.record_composer_telemetry_event(
        user_id="u_1",
        event_id=event_id,
        occurred_at=received,
        received_at=received + timedelta(days=1),
    )
    with pytest.raises(ValueError, match="outside accepted window"):
        await store.record_composer_telemetry_event(
            user_id="u_1",
            event_id="018f0cc3-e4a2-7b21-a109-001122334532",
            occurred_at=received - timedelta(days=30, seconds=1),
            received_at=received,
        )

    assert (
        await store.cleanup_composer_telemetry_dedupe(
            now=received + timedelta(days=30, hours=2),
        )
        == 0
    )
    assert (
        await store.cleanup_composer_telemetry_dedupe(
            now=received + timedelta(days=30, hours=2, seconds=1),
        )
        == 1
    )


@pytest.mark.asyncio
async def test_cross_scope_abandon_cannot_delete_an_inflight_claim(db_path: str) -> None:
    store = SessionStore(db_path)
    common = {
        "team_id": "team_1",
        "user_id": "u_1",
        "client_submission_id": "018f0cc3-e4a2-7b21-a109-001122334512",
        "workspace_id": "ws_1",
        "requested_session_id": None,
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "request_fingerprint": "3" * 64,
        "accepted_request_fingerprints": ("3" * 64,),
        "fingerprint_key_id": "f" * 64,
        "auth_session_generation_hash": "0" * 64,
    }
    assert (await store.claim_chat_submission(**common)).kind == "claimed"

    outcome = await store.abandon_chat_submission(
        team_id="team_1",
        user_id="u_1",
        client_submission_id=common["client_submission_id"],
        workspace_id="ws_other",
        requested_session_id=None,
    )

    assert outcome == "not_found"
    assert (await store.claim_chat_submission(**common)).kind == "pending"


@pytest.mark.asyncio
async def test_canonical_replay_source_keeps_attachment_ids(db_path: str) -> None:
    store = SessionStore(db_path)
    common = {
        "team_id": "team_1",
        "user_id": "u_1",
        "client_submission_id": "018f0cc3-e4a2-7b21-a109-001122334513",
        "workspace_id": "ws_1",
        "requested_session_id": None,
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "request_fingerprint": "4" * 64,
        "accepted_request_fingerprints": ("4" * 64,),
        "fingerprint_key_id": "f" * 64,
        "auth_session_generation_hash": "0" * 64,
    }
    claim = await store.claim_chat_submission(**common)
    await store.finalize_started_chat_submission(
        **_finalize_args(common),
        actual_session_id="s_source",
        turn_id="t_source",
        user_text="canonical text",
        attachment_ids=["art_1", "art_2"],
        lease_owner=claim.lease_owner,
        lease_version=claim.lease_version,
        response={"status": "started"},
    )

    source = await store.get_chat_turn_user_source(
        turn_id="t_source",
        user_id="u_1",
        workspace_id="ws_1",
    )

    assert source is not None
    assert source.user_text == "canonical text"
    assert source.attachment_ids == ("art_1", "art_2")
    assert await store.chat_submission_protocol_schema_ready() is True


@pytest.mark.asyncio
async def test_durable_cancel_is_sticky_against_late_completion(db_path: str) -> None:
    store = SessionStore(db_path)
    await store.ensure_session(session_id="s_cancel", workspace_id="ws_1", user_id="u_1")
    await store.record_turn(turn_id="t_cancel", session_id="s_cancel", user_text="cancel me")

    assert (
        await store.request_cancel_turn(
            team_id="team_1",
            user_id="u_1",
            cancel_request_id="018f0cc3-e4a2-7b21-a109-001122334590",
            workspace_id="ws_1",
            turn_id="t_cancel",
            expected_execution_generation=1,
        )
        == "cancelled"
    )
    await store.mark_completed(
        turn_id="t_cancel",
        intent=None,
        final_answer="late answer",
        tokens_input=1,
        tokens_output=1,
        cost_usd=0.0,
    )
    assert (
        await store.mark_failed(
            turn_id="t_cancel",
            error="late dispatch failure",
            error_kind="dispatch_start_failed",
        )
        is False
    )

    turn = await store.get_turn("t_cancel")
    assert turn is not None and turn.status == "cancelled"
    assert await store.get_active_turn("s_cancel") is None


@pytest.mark.asyncio
async def test_expired_receipt_is_retained_while_its_turn_is_nonterminal(db_path: str) -> None:
    store = SessionStore(db_path)
    common = {
        "team_id": "team_1",
        "user_id": "u_1",
        "client_submission_id": "018f0cc3-e4a2-7b21-a109-001122334514",
        "workspace_id": "ws_1",
        "requested_session_id": None,
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "request_fingerprint": "5" * 64,
        "accepted_request_fingerprints": ("5" * 64,),
        "fingerprint_key_id": "f" * 64,
        "auth_session_generation_hash": "0" * 64,
    }
    claim = await store.claim_chat_submission(**common)
    await store.finalize_started_chat_submission(
        **_finalize_args(common),
        actual_session_id="s_retained",
        turn_id="t_retained",
        user_text="long running",
        attachment_ids=[],
        lease_owner=claim.lease_owner,
        lease_version=claim.lease_version,
        response={"status": "started"},
    )
    expired = (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE chat_submission_receipts SET expires_at=? WHERE client_submission_id=?",
            (expired, common["client_submission_id"]),
        )
        connection.commit()

    assert await store.cleanup_chat_submission_protocol() == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_submission_receipts WHERE client_submission_id=?",
            (common["client_submission_id"],),
        ).fetchone() == (1,)

    assert (
        await store.mark_failed(
            turn_id="t_retained",
            error="terminal",
            error_kind="test",
        )
        is True
    )
    assert await store.cleanup_chat_submission_protocol() == 1


@pytest.mark.asyncio
async def test_initialize_preserves_a_turn_with_a_live_dispatch_lease(db_path: str) -> None:
    store = SessionStore(db_path)
    common = {
        "team_id": "team_1",
        "user_id": "u_1",
        "client_submission_id": "018f0cc3-e4a2-7b21-a109-001122334515",
        "workspace_id": "ws_1",
        "requested_session_id": None,
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "request_fingerprint": "6" * 64,
        "accepted_request_fingerprints": ("6" * 64,),
        "fingerprint_key_id": "f" * 64,
        "auth_session_generation_hash": "0" * 64,
    }
    claim = await store.claim_chat_submission(**common)
    await store.finalize_started_chat_submission(
        **_finalize_args(common),
        actual_session_id="s_live_lease",
        turn_id="t_live_lease",
        user_text="still owned",
        attachment_ids=[],
        lease_owner=claim.lease_owner,
        lease_version=claim.lease_version,
        response={"status": "started"},
    )

    await SessionStore(db_path).initialize()

    turn = await store.get_turn("t_live_lease")
    assert turn is not None and turn.status == "running"
    assert await store.get_active_turn("s_live_lease") == "t_live_lease"


@pytest.mark.asyncio
async def test_cancelled_dispatch_cannot_be_claimed_or_owner_stolen(db_path: str) -> None:
    store = SessionStore(db_path)
    common = {
        "team_id": "team_1",
        "user_id": "u_1",
        "client_submission_id": "018f0cc3-e4a2-7b21-a109-001122334516",
        "workspace_id": "ws_1",
        "requested_session_id": None,
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "request_fingerprint": "7" * 64,
        "accepted_request_fingerprints": ("7" * 64,),
        "fingerprint_key_id": "f" * 64,
        "auth_session_generation_hash": "0" * 64,
    }
    claim = await store.claim_chat_submission(**common)
    await store.finalize_started_chat_submission(
        **_finalize_args(common),
        actual_session_id="s_cancel_race",
        turn_id="t_cancel_race",
        user_text="cancel before dispatch",
        attachment_ids=[],
        lease_owner=claim.lease_owner,
        lease_version=claim.lease_version,
        response={"status": "started"},
    )

    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id="t_cancel_race",
            owner_id="owner-a",
        )
        == "claimed"
    )
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id="t_cancel_race",
            owner_id="owner-b",
        )
        == "rejected"
    )
    assert (
        await store.request_cancel_turn(
            team_id="team_1",
            user_id="u_1",
            cancel_request_id="018f0cc3-e4a2-7b21-a109-001122334591",
            workspace_id="ws_1",
            turn_id="t_cancel_race",
            expected_execution_generation=1,
        )
        == "cancelled"
    )
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id="t_cancel_race",
            owner_id="owner-b",
        )
        == "rejected"
    )
    assert await store.get_active_turn("s_cancel_race") is None
    with sqlite3.connect(db_path) as connection:
        dispatch = connection.execute(
            "SELECT state,cancel_requested_at FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            ("t_cancel_race",),
        ).fetchone()
    assert dispatch is not None
    assert dispatch[0] == "spawned"
    assert dispatch[1] is not None
    assert (
        await store.heartbeat_chat_spawn_dispatch(
            turn_id="t_cancel_race",
            owner_id="owner-a",
        )
        == "cancel_requested"
    )
    # Once the worker has observed cancellation and exited, its ordered event
    # tail remains live work. Settlement heartbeat must keep that same owner
    # leased until the tail is quiescent instead of letting recovery race it.
    assert (
        await store.heartbeat_chat_spawn_dispatch(
            turn_id="t_cancel_race",
            owner_id="owner-a",
            settling=True,
        )
        == "renewed"
    )


@pytest.mark.asyncio
async def test_expired_dispatch_fences_late_worker_terminal_write(db_path: str) -> None:
    store = SessionStore(db_path)
    log_store = TurnLogStore(db_path)
    await log_store.initialize()
    assert (
        await store.get_chat_spawn_dispatch_projection_state(
            turn_id="t_dispatch_missing",
        )
        == "untracked"
    )
    common = {
        "team_id": "team_1",
        "user_id": "u_1",
        "client_submission_id": "018f0cc3-e4a2-7b21-a109-001122334517",
        "workspace_id": "ws_1",
        "requested_session_id": None,
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "request_fingerprint": "8" * 64,
        "accepted_request_fingerprints": ("8" * 64,),
        "fingerprint_key_id": "f" * 64,
        "auth_session_generation_hash": "0" * 64,
    }
    claim = await store.claim_chat_submission(**common)
    await store.finalize_started_chat_submission(
        **_finalize_args(common),
        actual_session_id="s_expired_owner",
        turn_id="t_expired_owner",
        user_text="lease expires",
        attachment_ids=[],
        lease_owner=claim.lease_owner,
        lease_version=claim.lease_version,
        response={"status": "started"},
    )
    assert (
        await store.get_chat_spawn_dispatch_projection_state(
            turn_id="t_expired_owner",
        )
        == "active"
    )
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id="t_expired_owner",
            owner_id="owner-a",
        )
        == "claimed"
    )
    assert (
        await log_store.record(
            turn_id="t_expired_owner",
            event_type="partial_answer",
            payload={"seq": 1, "evt_seq": 1, "text": "before recovery"},
        )
        is not None
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
            ((datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat(), "t_expired_owner"),
        )
        connection.commit()

    late_record_started = asyncio.Event()
    allow_late_record = asyncio.Event()

    class _GatedTurnLogStore:
        async def record(self, **kwargs):
            late_record_started.set()
            await allow_late_record.wait()
            return await log_store.record(**kwargs)

    ordered_sink = OrderedTurnLogSink(_GatedTurnLogStore())  # type: ignore[arg-type]
    registry = EventBusRegistry(default_log_sink=ordered_sink)
    bus = await registry.get_or_create("t_expired_owner", persist_before_live=True)
    late_emit = asyncio.create_task(
        bus.emit(
            OrchestratorEvent(
                event_type="partial_answer",
                turn_id="t_expired_owner",
                payload={"text": "late owner frame"},
            ),
        ),
    )
    await late_record_started.wait()
    assert bus.snapshot() == []

    assert await store.recover_expired_chat_spawn_dispatches() == ["t_expired_owner"]
    allow_late_record.set()
    with pytest.raises(
        TerminalDurabilityAbort,
        match="legacy durable event write has failed",
    ):
        await late_emit
    await ordered_sink.drain()
    assert bus.snapshot() == []
    assert (
        await store.get_chat_spawn_dispatch_projection_state(
            turn_id="t_expired_owner",
        )
        == "settled"
    )
    assert (
        await log_store.record(
            turn_id="t_expired_owner",
            event_type="done",
            payload={"seq": 2, "evt_seq": 2, "status": "failed"},
        )
        is None
    )
    recovered_events = await log_store.list_events("t_expired_owner")
    assert [row.event_type for row in recovered_events] == [
        "partial_answer",
        "turn_failed",
        "done",
    ]
    assert recovered_events[-2].payload["reason"] == "turn_dispatch_owner_lost"
    # Owner process lost (service upgrade/restart) ⇒ tell users 「被打断」, not 「系统处理出错」.
    assert recovered_events[-2].payload["reason_code"] == "service_interrupted"
    assert recovered_events[-1].payload["status"] == "failed"
    # Chat turn feedback D5: recovery writes these rows by SQL, outside the bus; the persisted
    # pair still carries the failure card's action.
    assert recovered_events[-2].payload["recovery_action"] == "retry"
    assert recovered_events[-1].payload["recovery_action"] == "retry"
    assert recovered_events[-1].payload["reason_code"] == "service_interrupted"
    await store.mark_completed(
        turn_id="t_expired_owner",
        intent=None,
        final_answer="late success must not win",
        tokens_input=1,
        tokens_output=1,
        cost_usd=0.0,
    )
    turn = await store.get_turn("t_expired_owner")
    assert turn is not None
    assert turn.status == "failed"
    assert turn.final_answer is None


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_event_type", ["error", "done"])
async def test_expired_dispatch_does_not_duplicate_existing_terminal_event(
    db_path: str,
    terminal_event_type: str,
) -> None:
    store = SessionStore(db_path)
    log_store = TurnLogStore(db_path)
    await log_store.initialize()
    turn_id = f"t_existing_{terminal_event_type}"
    session_id = f"s_existing_{terminal_event_type}"
    await store.ensure_session(
        session_id=session_id,
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id=turn_id,
        session_id=session_id,
        user_text="terminal already durable",
    )
    now = datetime.now(tz=UTC).isoformat()
    expired = (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO chat_turn_spawn_dispatch("
            "turn_id,session_id,source_submission_ids_json,dispatch_kind,state,"
            "owner_id,lease_expires_at,created_at,updated_at"
            ") VALUES (?,?,?,'start','spawned',?,?,?,?)",
            (
                turn_id,
                session_id,
                "[]",
                "owner-lost",
                expired,
                now,
                now,
            ),
        )
        connection.execute(
            "UPDATE turns SET status='failed',error_kind='dispatch_owner_lost',error='turn dispatch owner was lost',ended_at=? WHERE id=?",
            (now, turn_id),
        )
        connection.commit()
    assert await log_store.record(
        turn_id=turn_id,
        event_type=terminal_event_type,
        payload={
            "turn_id": turn_id,
            "seq": 1,
            "evt_seq": 1,
            **({"status": "failed"} if terminal_event_type == "done" else {}),
        },
    )

    assert await store.recover_expired_chat_spawn_dispatches() == [turn_id]
    events = await log_store.list_events(turn_id)
    assert [event.event_type for event in events] == [terminal_event_type]
    assert (
        await store.get_chat_spawn_dispatch_projection_state(
            turn_id=turn_id,
        )
        == "settled"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    [
        "completed",
        "failed",
        "cancelled",
        "aborted",
        "clarification_pending",
        "awaiting_user",
        "awaiting_approval",
        "approval_pending",
    ],
)
async def test_orphan_terminal_buffer_is_fail_closed_and_bootstrapped_after_grace(
    db_path: str,
    terminal_status: str,
) -> None:
    store = SessionStore(db_path)
    turn_id = f"t_orphan_{terminal_status}"
    session_id = f"s_orphan_{terminal_status}"
    await store.ensure_session(
        session_id=session_id,
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id=turn_id,
        session_id=session_id,
        user_text="legacy parent",
    )
    assert await store.append_steer_buffer(
        session_id=session_id,
        expected_turn_id=turn_id,
        text="accepted follow-up",
    )
    old_terminal = (datetime.now(tz=UTC) - timedelta(minutes=2)).isoformat()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turns SET status=?,ended_at=? WHERE id=?",
            (terminal_status, old_terminal, turn_id),
        )
        connection.commit()

    assert (
        await store.get_chat_spawn_dispatch_projection_state(
            turn_id=turn_id,
        )
        == "active"
    )
    assert await store.recover_expired_chat_spawn_dispatches() == [turn_id]
    assert await store.get_chat_follow_up(parent_turn_id=turn_id) is not None


@pytest.mark.asyncio
async def test_orphan_terminal_empty_buffer_atomically_closes_steer_admission(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_orphan_empty",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_orphan_empty",
        session_id="s_orphan_empty",
        user_text="legacy parent",
    )
    old_terminal = (datetime.now(tz=UTC) - timedelta(minutes=2)).isoformat()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turns SET status='completed',ended_at=? WHERE id='t_orphan_empty'",
            (old_terminal,),
        )
        connection.commit()

    recovery, appended = await asyncio.gather(
        store.recover_expired_chat_spawn_dispatches(),
        store.append_steer_buffer(
            session_id="s_orphan_empty",
            expected_turn_id="t_orphan_empty",
            text="racing follow-up",
        ),
    )
    assert recovery == ["t_orphan_empty"]
    assert await store.get_active_turn("s_orphan_empty") is None
    if appended:
        assert await store.get_chat_follow_up(parent_turn_id="t_orphan_empty") is not None
    else:
        with sqlite3.connect(db_path) as connection:
            assert connection.execute(
                "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id='t_orphan_empty'",
            ).fetchone() == ("failed",)
            done = connection.execute(
                "SELECT payload_json FROM turn_events WHERE turn_id='t_orphan_empty' AND event_type='done' ORDER BY seq DESC LIMIT 1",
            ).fetchone()
        assert done is not None
        assert json.loads(done[0])["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dispatch_state", "buffer_json", "expects_child"),
    [
        ("terminal", '[{"text":"accepted"}]', True),
        ("failed", "not-json", False),
    ],
)
async def test_existing_settled_dispatch_is_promoted_for_orphan_buffer_recovery(
    db_path: str,
    dispatch_state: str,
    buffer_json: str,
    expects_child: bool,
) -> None:
    store = SessionStore(db_path)
    turn_id = f"t_existing_settled_{dispatch_state}"
    session_id = f"s_existing_settled_{dispatch_state}"
    await store.ensure_session(
        session_id=session_id,
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id=turn_id,
        session_id=session_id,
        user_text="legacy parent",
    )
    old_terminal = (datetime.now(tz=UTC) - timedelta(minutes=2)).isoformat()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE sessions SET steer_buffer_json=? WHERE id=?",
            (buffer_json, session_id),
        )
        connection.execute(
            "UPDATE turns SET status='completed',ended_at=? WHERE id=?",
            (old_terminal, turn_id),
        )
        connection.execute(
            "INSERT INTO chat_turn_spawn_dispatch("
            "turn_id,session_id,source_submission_ids_json,dispatch_kind,state,"
            "owner_id,lease_expires_at,created_at,updated_at"
            ") VALUES (?,?,?,'start',?,NULL,?,?,?)",
            (
                turn_id,
                session_id,
                "[]",
                dispatch_state,
                old_terminal,
                old_terminal,
                old_terminal,
            ),
        )
        connection.commit()

    assert await store.recover_expired_chat_spawn_dispatches() == [turn_id]
    assert await store.get_active_turn(session_id) is None
    child = await store.get_chat_follow_up(parent_turn_id=turn_id)
    assert (child is not None) is expects_child
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == (("failed",) if not expects_child else ("failed",))


@pytest.mark.asyncio
async def test_fresh_orphan_terminal_buffer_waits_for_worker_tail_grace(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_fresh_orphan",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_fresh_orphan",
        session_id="s_fresh_orphan",
        user_text="legacy parent",
    )
    assert await store.append_steer_buffer(
        session_id="s_fresh_orphan",
        expected_turn_id="t_fresh_orphan",
        text="healthy worker still owns tail",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turns SET status='completed',started_at=?,ended_at=? WHERE id='t_fresh_orphan'",
            (
                (datetime.now(tz=UTC) - timedelta(minutes=20)).isoformat(),
                datetime.now(tz=UTC).isoformat(),
            ),
        )
        connection.commit()

    assert (
        await store.get_chat_spawn_dispatch_projection_state(
            turn_id="t_fresh_orphan",
        )
        == "active"
    )
    assert await store.recover_expired_chat_spawn_dispatches() == []
    assert await store.cleanup_stale_running_turns(older_than_min=10) == 0
    assert await store.get_active_turn("s_fresh_orphan") == "t_fresh_orphan"
    with sqlite3.connect(db_path) as connection:
        buffered_json = connection.execute(
            "SELECT steer_buffer_json FROM sessions WHERE id='s_fresh_orphan'",
        ).fetchone()[0]
        assert (
            connection.execute(
                "SELECT 1 FROM chat_turn_spawn_dispatch WHERE turn_id='t_fresh_orphan'",
            ).fetchone()
            is None
        )
    assert [item["text"] for item in json.loads(buffered_json)] == [
        "healthy worker still owns tail",
    ]

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turns SET ended_at=? WHERE id='t_fresh_orphan'",
            ((datetime.now(tz=UTC) - timedelta(minutes=2)).isoformat(),),
        )
        connection.commit()
    assert await store.recover_expired_chat_spawn_dispatches() == [
        "t_fresh_orphan",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed_buffer",
    ["not-json", "{}", '"text"', "null"],
)
async def test_malformed_orphan_buffer_is_active_until_recovery_isolates_it(
    db_path: str,
    malformed_buffer: str,
) -> None:
    store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_malformed_orphan",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_malformed_orphan",
        session_id="s_malformed_orphan",
        user_text="legacy parent",
    )
    old_terminal = (datetime.now(tz=UTC) - timedelta(minutes=2)).isoformat()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE sessions SET steer_buffer_json=? WHERE id=?",
            (malformed_buffer, "s_malformed_orphan"),
        )
        connection.execute(
            "UPDATE turns SET status='completed',ended_at=? WHERE id=?",
            (old_terminal, "t_malformed_orphan"),
        )
        connection.commit()

    assert (
        await store.get_chat_spawn_dispatch_projection_state(
            turn_id="t_malformed_orphan",
        )
        == "active"
    )
    assert await store.recover_expired_chat_spawn_dispatches() == [
        "t_malformed_orphan",
    ]
    assert await store.recover_expired_chat_spawn_dispatches() == []

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id='t_malformed_orphan'",
        ).fetchone() == ("failed",)
        assert connection.execute(
            "SELECT active_turn_id,steer_buffer_json FROM sessions WHERE id='s_malformed_orphan'",
        ).fetchone() == (None, "[]")


@pytest.mark.asyncio
async def test_drain_marker_treats_non_array_buffer_as_unknown_work(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_non_array_drain",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_non_array_drain",
        session_id="s_non_array_drain",
        user_text="legacy parent",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE sessions SET steer_buffer_json='{}' WHERE id=?",
            ("s_non_array_drain",),
        )
        connection.commit()

    assert await store.mark_chat_spawn_dispatch_drain_pending(
        turn_id="t_non_array_drain",
        session_id="s_non_array_drain",
    )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id='t_non_array_drain'",
        ).fetchone() == ("drain_pending",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed_buffer",
    ["not-json", "{}", '"text"', "null"],
)
async def test_stale_running_malformed_buffer_enters_recovery_without_looping(
    db_path: str,
    malformed_buffer: str,
) -> None:
    store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_malformed_stale",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_malformed_stale",
        session_id="s_malformed_stale",
        user_text="legacy parent",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE sessions SET steer_buffer_json=? WHERE id=?",
            (malformed_buffer, "s_malformed_stale"),
        )
        connection.execute(
            "UPDATE turns SET started_at=? WHERE id=?",
            (
                (datetime.now(tz=UTC) - timedelta(minutes=20)).isoformat(),
                "t_malformed_stale",
            ),
        )
        connection.commit()

    assert await store.cleanup_stale_running_turns(older_than_min=10) == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id='t_malformed_stale'",
        ).fetchone() == ("drain_pending",)
        assert connection.execute(
            "SELECT active_turn_id,steer_buffer_json FROM sessions WHERE id='s_malformed_stale'",
        ).fetchone() == ("t_malformed_stale", malformed_buffer)

    assert await store.recover_expired_chat_spawn_dispatches() == [
        "t_malformed_stale",
    ]
    assert await store.recover_expired_chat_spawn_dispatches() == []
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id='t_malformed_stale'",
        ).fetchone() == ("failed",)
        assert connection.execute(
            "SELECT active_turn_id,steer_buffer_json FROM sessions WHERE id='s_malformed_stale'",
        ).fetchone() == (None, "[]")


@pytest.mark.asyncio
async def test_dispatch_recovery_isolates_bad_row_and_continues_next_row(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    for suffix in ("bad", "good"):
        await store.ensure_session(
            session_id=f"s_recovery_{suffix}",
            workspace_id="ws_1",
            user_id="u_1",
        )
        await store.record_turn(
            turn_id=f"t_recovery_{suffix}",
            session_id=f"s_recovery_{suffix}",
            user_text=f"{suffix} parent",
        )
    assert await store.append_steer_buffer(
        session_id="s_recovery_good",
        expected_turn_id="t_recovery_good",
        text="recover me",
    )
    now = datetime.now(tz=UTC).isoformat()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE sessions SET steer_buffer_json='not-json' WHERE id='s_recovery_bad'",
        )
        connection.executemany(
            "INSERT INTO chat_turn_spawn_dispatch("
            "turn_id,session_id,source_submission_ids_json,dispatch_kind,state,"
            "lease_expires_at,created_at,updated_at"
            ") VALUES (?,?,'[]','start','drain_pending',?,?,?)",
            [
                ("t_recovery_bad", "s_recovery_bad", now, now, now),
                ("t_recovery_good", "s_recovery_good", now, now, now),
            ],
        )
        connection.commit()

    assert set(await store.recover_expired_chat_spawn_dispatches()) == {
        "t_recovery_bad",
        "t_recovery_good",
    }
    assert await store.recover_expired_chat_spawn_dispatches() == []
    assert await store.get_chat_follow_up(parent_turn_id="t_recovery_bad") is None
    assert await store.get_chat_follow_up(parent_turn_id="t_recovery_good") is not None
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT active_turn_id,steer_buffer_json FROM sessions WHERE id='s_recovery_bad'",
        ).fetchone() == (None, "[]")
        assert connection.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id='t_recovery_bad'",
        ).fetchone() == ("failed",)


@pytest.mark.asyncio
async def test_dispatch_recovery_rolls_back_mid_row_conflict_and_commits_good_row(
    db_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(db_path)
    for suffix, text in (
        ("conflict", "trigger deterministic conflict"),
        ("good_after_conflict", "recover after conflict"),
    ):
        await store.ensure_session(
            session_id=f"s_{suffix}",
            workspace_id="ws_1",
            user_id="u_1",
        )
        await store.record_turn(
            turn_id=f"t_{suffix}",
            session_id=f"s_{suffix}",
            user_text="parent",
        )
        assert await store.append_steer_buffer(
            session_id=f"s_{suffix}",
            expected_turn_id=f"t_{suffix}",
            text=text,
        )
    now = datetime.now(tz=UTC).isoformat()
    with sqlite3.connect(db_path) as connection:
        connection.executemany(
            "INSERT INTO chat_turn_spawn_dispatch("
            "turn_id,session_id,source_submission_ids_json,dispatch_kind,state,"
            "lease_expires_at,created_at,updated_at"
            ") VALUES (?,?,'[]','start','drain_pending',?,?,?)",
            [
                ("t_conflict", "s_conflict", now, now, now),
                (
                    "t_good_after_conflict",
                    "s_good_after_conflict",
                    now,
                    now,
                    now,
                ),
            ],
        )
        connection.commit()

    original_insert = session_store_module._insert_chat_turn_user_source_in_tx

    async def _raise_for_conflicting_source(*args, **kwargs):
        source = kwargs["source"]
        if source.user_text == "trigger deterministic conflict":
            raise ValueError("deterministic recovery source conflict")
        return await original_insert(*args, **kwargs)

    monkeypatch.setattr(
        session_store_module,
        "_insert_chat_turn_user_source_in_tx",
        _raise_for_conflicting_source,
    )

    assert set(await store.recover_expired_chat_spawn_dispatches()) == {
        "t_conflict",
        "t_good_after_conflict",
    }
    assert await store.get_chat_follow_up(parent_turn_id="t_conflict") is None
    assert (
        await store.get_chat_follow_up(
            parent_turn_id="t_good_after_conflict",
        )
        is not None
    )
    assert await store.recover_expired_chat_spawn_dispatches() == []
    with sqlite3.connect(db_path) as connection:
        isolated_before = connection.execute(
            "SELECT active_turn_id,steer_buffer_json FROM sessions WHERE id='s_conflict'",
        ).fetchone()
    assert isolated_before is not None
    assert isolated_before[0] == "t_conflict"
    assert [item["text"] for item in json.loads(isolated_before[1])] == [
        "trigger deterministic conflict",
    ]
    assert not await store.append_steer_buffer(
        session_id="s_conflict",
        expected_turn_id="t_conflict",
        text="must not be acknowledged after isolation",
    )
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT active_turn_id,steer_buffer_json FROM sessions WHERE id='s_conflict'",
            ).fetchone()
            == isolated_before
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_type",
    [sqlite3.OperationalError, asyncio.CancelledError],
)
async def test_dispatch_recovery_rolls_back_whole_batch_on_system_failure(
    db_path: str,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
) -> None:
    store = SessionStore(db_path)
    now = datetime.now(tz=UTC).isoformat()
    for suffix in ("first", "second"):
        await store.ensure_session(
            session_id=f"s_system_failure_{suffix}",
            workspace_id="ws_1",
            user_id="u_1",
        )
        await store.record_turn(
            turn_id=f"t_system_failure_{suffix}",
            session_id=f"s_system_failure_{suffix}",
            user_text="parent",
        )
        assert await store.append_steer_buffer(
            session_id=f"s_system_failure_{suffix}",
            expected_turn_id=f"t_system_failure_{suffix}",
            text=f"recover {suffix}",
        )
    with sqlite3.connect(db_path) as connection:
        connection.executemany(
            "INSERT INTO chat_turn_spawn_dispatch("
            "turn_id,session_id,source_submission_ids_json,dispatch_kind,state,"
            "lease_expires_at,created_at,updated_at"
            ") VALUES (?,?,'[]','start','drain_pending',?,?,?)",
            [
                (
                    f"t_system_failure_{suffix}",
                    f"s_system_failure_{suffix}",
                    now,
                    now,
                    now,
                )
                for suffix in ("first", "second")
            ],
        )
        connection.commit()
        before = {
            "turns": connection.execute(
                "SELECT id,status,ended_at,error,error_kind FROM turns WHERE id LIKE 't_system_failure_%' ORDER BY id",
            ).fetchall(),
            "sessions": connection.execute(
                "SELECT id,active_turn_id,steer_buffer_json FROM sessions WHERE id LIKE 's_system_failure_%' ORDER BY id",
            ).fetchall(),
            "dispatch": connection.execute(
                "SELECT turn_id,state,owner_id FROM chat_turn_spawn_dispatch WHERE turn_id LIKE 't_system_failure_%' ORDER BY turn_id",
            ).fetchall(),
        }

    calls = 0
    if failure_type is sqlite3.OperationalError:
        original_execute = aiosqlite.Connection.execute

        def _fail_second_row_schema_read(self, sql, parameters=None):
            nonlocal calls
            if str(sql).strip() == "PRAGMA table_info(turns)":
                calls += 1
                if calls == 3:
                    raise sqlite3.OperationalError(
                        "injected schema inspection failure",
                    )
            return original_execute(self, sql, parameters)

        monkeypatch.setattr(
            aiosqlite.Connection,
            "execute",
            _fail_second_row_schema_read,
        )
        expected_calls = 3
    else:
        original_reader = session_store_module._read_legacy_continuation_parent_in_tx

        async def _fail_second_row(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise asyncio.CancelledError("injected system failure")
            return await original_reader(*args, **kwargs)

        monkeypatch.setattr(
            session_store_module,
            "_read_legacy_continuation_parent_in_tx",
            _fail_second_row,
        )
        expected_calls = 2

    with pytest.raises(failure_type):
        await store.recover_expired_chat_spawn_dispatches()
    assert calls == expected_calls

    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT id,status,ended_at,error,error_kind FROM turns WHERE id LIKE 't_system_failure_%' ORDER BY id",
            ).fetchall()
            == before["turns"]
        )
        assert (
            connection.execute(
                "SELECT id,active_turn_id,steer_buffer_json FROM sessions WHERE id LIKE 's_system_failure_%' ORDER BY id",
            ).fetchall()
            == before["sessions"]
        )
        assert (
            connection.execute(
                "SELECT turn_id,state,owner_id FROM chat_turn_spawn_dispatch WHERE turn_id LIKE 't_system_failure_%' ORDER BY turn_id",
            ).fetchall()
            == before["dispatch"]
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM turns WHERE id LIKE 't_%' AND id NOT IN ('t_system_failure_first','t_system_failure_second')",
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_spawn_failure_atomically_preserves_concurrent_accepted_steer(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_spawn_failure_steer",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_spawn_failure_steer",
        session_id="s_spawn_failure_steer",
        user_text="parent",
    )
    now = datetime.now(tz=UTC).isoformat()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO chat_turn_spawn_dispatch("
            "turn_id,session_id,source_submission_ids_json,dispatch_kind,state,"
            "owner_id,lease_expires_at,created_at,updated_at"
            ") VALUES (?,?,?,'start','spawned',?,?,?,?)",
            (
                "t_spawn_failure_steer",
                "s_spawn_failure_steer",
                "[]",
                "owner-a",
                (datetime.now(tz=UTC) + timedelta(seconds=30)).isoformat(),
                now,
                now,
            ),
        )
        connection.commit()
    assert await store.append_steer_buffer(
        session_id="s_spawn_failure_steer",
        expected_turn_id="t_spawn_failure_steer",
        text="accepted during receipt retry",
    )
    assert (
        await store.prepare_chat_spawn_failure(
            turn_id="t_spawn_failure_steer",
            session_id="s_spawn_failure_steer",
            expected_owner_id="owner-a",
        )
        == "ready"
    )
    assert await store.get_active_turn("s_spawn_failure_steer") == "t_spawn_failure_steer"
    assert await store.mark_failed(
        turn_id="t_spawn_failure_steer",
        error="turn dispatch failed before worker start",
        error_kind="dispatch_start_failed",
    )
    assert await store.mark_chat_spawn_dispatch_drain_pending(
        turn_id="t_spawn_failure_steer",
        owner_id="owner-a",
        session_id="s_spawn_failure_steer",
    )
    assert await store.recover_expired_chat_spawn_dispatches() == [
        "t_spawn_failure_steer",
    ]
    assert (
        await store.get_chat_follow_up(
            parent_turn_id="t_spawn_failure_steer",
        )
        is not None
    )


@pytest.mark.asyncio
async def test_spawn_failure_does_not_mutate_foreign_live_owner(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_spawn_failure_foreign",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_spawn_failure_foreign",
        session_id="s_spawn_failure_foreign",
        user_text="parent",
    )
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id="t_spawn_failure_foreign",
            owner_id="owner-a",
        )
        == "claimed"
    )
    assert await store.append_steer_buffer(
        session_id="s_spawn_failure_foreign",
        expected_turn_id="t_spawn_failure_foreign",
        text="accepted by live owner",
    )

    assert (
        await store.prepare_chat_spawn_failure(
            turn_id="t_spawn_failure_foreign",
            session_id="s_spawn_failure_foreign",
            expected_owner_id="owner-b",
        )
        == "foreign_active"
    )
    assert (
        await store.heartbeat_chat_spawn_dispatch(
            turn_id="t_spawn_failure_foreign",
            owner_id="owner-a",
        )
        == "renewed"
    )
    turn = await store.get_turn("t_spawn_failure_foreign")
    assert turn is not None and turn.status == "running"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state,owner_id FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            ("t_spawn_failure_foreign",),
        ).fetchone() == ("spawned", "owner-a")


@pytest.mark.asyncio
async def test_spawn_failure_prepares_empty_turn_recovery_before_terminal(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    competing_store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_spawn_failure_empty",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_spawn_failure_empty",
        session_id="s_spawn_failure_empty",
        user_text="parent",
    )
    # Reproduce an old persisted Turn whose process-local admission never
    # committed the Session pointer; recovery must claim it atomically.
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE sessions SET active_turn_id=NULL WHERE id='s_spawn_failure_empty'",
        )
        connection.commit()

    assert (
        await store.prepare_chat_spawn_failure(
            turn_id="t_spawn_failure_empty",
            session_id="s_spawn_failure_empty",
            expected_owner_id="owner-a",
        )
        == "ready"
    )
    assert await store.get_active_turn("s_spawn_failure_empty") == ("t_spawn_failure_empty")
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state,lease_kind,lease_execution_generation,owner_id FROM chat_turn_spawn_dispatch WHERE turn_id='t_spawn_failure_empty'",
        ).fetchone() == ("spawned", "turn", 1, "owner-a")

    # Exact critical window: a second connection attempts to claim after the
    # preparation transaction commits but before the first caller writes the
    # terminal Turn fact. The recovery owner must remain exclusive.
    assert (
        await competing_store.claim_chat_spawn_dispatch_for_worker(
            turn_id="t_spawn_failure_empty",
            owner_id="owner-b",
        )
        == "rejected"
    )

    assert await store.mark_failed(
        turn_id="t_spawn_failure_empty",
        error="turn dispatch claim failed before worker start",
        error_kind="dispatch_start_failed",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
            (
                (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat(),
                "t_spawn_failure_empty",
            ),
        )
        connection.commit()
    assert await store.recover_expired_chat_spawn_dispatches() == [
        "t_spawn_failure_empty",
    ]
    assert await store.get_active_turn("s_spawn_failure_empty") is None


@pytest.mark.asyncio
async def test_expired_dispatch_uses_turn_session_when_marker_session_is_wrong(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_dispatch_actual",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.ensure_session(
        session_id="s_dispatch_wrong",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_dispatch_wrong_session",
        session_id="s_dispatch_actual",
        user_text="parent",
    )
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id="t_dispatch_wrong_session",
            owner_id="owner-a",
        )
        == "claimed"
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET session_id=?,lease_expires_at=? WHERE turn_id=?",
            (
                "s_dispatch_wrong",
                (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat(),
                "t_dispatch_wrong_session",
            ),
        )
        connection.commit()

    assert await store.recover_expired_chat_spawn_dispatches() == [
        "t_dispatch_wrong_session",
    ]
    turn = await store.get_turn("t_dispatch_wrong_session")
    assert turn is not None and turn.status == "failed"
    assert await store.get_active_turn("s_dispatch_actual") is None
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id='t_dispatch_wrong_session'",
        ).fetchone() == ("failed",)
        assert connection.execute(
            "SELECT COUNT(*) FROM turn_events WHERE turn_id=? AND event_type='done'",
            ("t_dispatch_wrong_session",),
        ).fetchone() == (1,)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["completed", "cancelled"])
async def test_expired_parent_dispatch_fences_late_writer(
    db_path: str,
    terminal_status: str,
) -> None:
    store = SessionStore(db_path)
    log_store = TurnLogStore(db_path)
    await log_store.initialize()
    await store.ensure_session(
        session_id=f"s_legacy_{terminal_status}",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id=f"t_legacy_{terminal_status}",
        session_id=f"s_legacy_{terminal_status}",
        user_text="legacy parent",
    )
    await store.append_steer_buffer(
        session_id=f"s_legacy_{terminal_status}",
        text="accepted follow-up",
    )
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id=f"t_legacy_{terminal_status}",
            owner_id="gateway-owner",
        )
        == "claimed"
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turns SET status=?,ended_at=? WHERE id=?",
            (
                terminal_status,
                datetime.now(tz=UTC).isoformat(),
                f"t_legacy_{terminal_status}",
            ),
        )
        connection.commit()

    assert await log_store.record(
        turn_id=f"t_legacy_{terminal_status}",
        event_type="partial_answer",
        payload={"text": "durable before recovery"},
    )
    assert await store.materialize_steer_follow_up(
        session_id=f"s_legacy_{terminal_status}",
        current_turn_id=f"t_legacy_{terminal_status}",
        next_turn_id=f"t_legacy_{terminal_status}_child",
    ) == ["accepted follow-up"]
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
            (
                (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat(),
                f"t_legacy_{terminal_status}",
            ),
        )
        connection.commit()

    assert await store.recover_expired_chat_spawn_dispatches() == [
        f"t_legacy_{terminal_status}",
    ]
    assert (
        await store.get_chat_spawn_dispatch_projection_state(
            turn_id=f"t_legacy_{terminal_status}",
        )
        == "settled"
    )
    assert (
        await log_store.record(
            turn_id=f"t_legacy_{terminal_status}",
            event_type="partial_answer",
            payload={"text": "stale worker tail"},
        )
        is None
    )
    events = await log_store.list_events(f"t_legacy_{terminal_status}")
    assert [row.payload["text"] for row in events if row.event_type == "partial_answer"] == ["durable before recovery"]
    assert [row.event_type for row in events].count("done") == 1
    turn = await store.get_turn(f"t_legacy_{terminal_status}")
    assert turn is not None
    assert turn.status == terminal_status


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_generation", [False, True])
async def test_peer_error_never_proves_root_terminal_for_dispatch_recovery(
    db_path: str,
    resume_generation: bool,
) -> None:
    store = SessionStore(db_path)
    log_store = TurnLogStore(db_path)
    await log_store.initialize()
    suffix = "resume" if resume_generation else "fresh"
    session_id = f"s_peer_error_{suffix}"
    turn_id = f"t_peer_error_{suffix}"
    await store.ensure_session(
        session_id=session_id,
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id=turn_id,
        session_id=session_id,
        user_text="complete despite a peer error",
    )

    generation_floor = 0
    if resume_generation:
        assert (
            await store.claim_chat_spawn_dispatch_for_worker(
                turn_id=turn_id,
                owner_id="pause-owner",
            )
            == "claimed"
        )
        assert await store.mark_clarification_pending(
            turn_id=turn_id,
            intent="clarify",
        )
        assert (
            await store.materialize_steer_follow_up(
                session_id=session_id,
                current_turn_id=turn_id,
                next_turn_id=f"unused_{turn_id}",
            )
            == []
        )
        assert await log_store.record(
            turn_id=turn_id,
            event_type="done",
            payload={
                "turn_id": turn_id,
                "status": "clarification_pending",
                "execution_generation": 1,
                "evt_seq": 1,
            },
        )
        assert await store.mark_chat_spawn_dispatch_terminal(
            turn_id=turn_id,
            owner_id="pause-owner",
        )
        assert await store.advance_turn_execution_generation(turn_id=turn_id) == 2
        assert (
            await store.claim_chat_spawn_dispatch_for_worker(
                turn_id=turn_id,
                owner_id="resume-owner",
            )
            == "claimed"
        )
        resumed = await log_store.record(
            turn_id=turn_id,
            event_type="turn.resumed",
            payload={
                "turn_id": turn_id,
                "execution_generation": 2,
                "evt_seq": 2,
            },
        )
        assert resumed is not None
        generation_floor = resumed.seq
    else:
        assert (
            await store.claim_chat_spawn_dispatch_for_worker(
                turn_id=turn_id,
                owner_id="fresh-owner",
            )
            == "claimed"
        )

    stale_final = await log_store.record(
        turn_id=turn_id,
        event_type="final_answer",
        payload={
            "turn_id": turn_id,
            "text": f"{suffix} stale answer before canonical correction",
            "card_id": turn_id,
            "evt_seq": generation_floor + 1,
        },
    )
    assert stale_final is not None
    peer_error = await log_store.record(
        turn_id=turn_id,
        event_type="error",
        payload={
            "turn_id": turn_id,
            "parent_agent_id": "lead",
            "subagent_id": "peer-1",
            "reason": "peer failed but root continued",
            "evt_seq": generation_floor + 2,
        },
    )
    assert peer_error is not None
    assert await store.mark_completed(
        turn_id=turn_id,
        intent="lookup",
        final_answer=f"{suffix} canonical answer",
        tokens_input=3,
        tokens_output=5,
        cost_usd=0.1,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
            (
                (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat(),
                turn_id,
            ),
        )
        connection.commit()

    assert await store.recover_expired_chat_spawn_dispatches() == [turn_id]
    events = await log_store.list_events(turn_id)
    current_events = [event for event in events if event.seq > generation_floor]
    assert [event.event_type for event in current_events] == [
        "final_answer",
        "error",
        "final_answer",
        "done",
    ]
    assert current_events[1].payload["parent_agent_id"] == "lead"
    assert current_events[-2].payload["text"] == f"{suffix} canonical answer"
    assert current_events[-1].payload["status"] == "completed"


@pytest.mark.asyncio
async def test_expired_claimed_follow_up_waits_for_live_parent_dispatch(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_live_parent",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_live_parent",
        session_id="s_live_parent",
        user_text="parent",
    )
    await store.append_steer_buffer(
        session_id="s_live_parent",
        text="accepted while parent tail is running",
    )
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id="t_live_parent",
            owner_id="parent-owner",
        )
        == "claimed"
    )
    assert await store.materialize_steer_follow_up(
        session_id="s_live_parent",
        current_turn_id="t_live_parent",
        next_turn_id="t_live_child",
    ) == ["accepted while parent tail is running"]
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
            (
                (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat(),
                "t_live_child",
            ),
        )
        connection.commit()

    # Parent terminal tails can legitimately outlive the child's initial
    # claimed lease. The live parent is still the only process authorized to
    # spawn that child, so maintenance must not convert it to owner-lost.
    assert await store.recover_expired_chat_spawn_dispatches() == []
    child = await store.get_turn("t_live_child")
    assert child is not None and child.status == "running"

    assert await store.mark_completed(
        turn_id="t_live_parent",
        intent="lookup",
        final_answer="done",
        tokens_input=1,
        tokens_output=1,
        cost_usd=0.0,
    )
    await _record_root_done(
        db_path,
        turn_id="t_live_parent",
        status="completed",
        final_answer="done",
    )
    assert await store.mark_chat_spawn_dispatch_terminal(
        turn_id="t_live_parent",
        owner_id="parent-owner",
    )
    assert await store.recover_expired_chat_spawn_dispatches() == ["t_live_child"]
    child = await store.get_turn("t_live_child")
    assert child is not None and child.status == "failed"


@pytest.mark.asyncio
async def test_expired_inactive_parent_reconstructs_done_without_touching_child(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    log_store = TurnLogStore(db_path)
    await log_store.initialize()
    await store.ensure_session(
        session_id="s_inactive_parent",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_inactive_parent",
        session_id="s_inactive_parent",
        user_text="parent",
    )
    await store.append_steer_buffer(
        session_id="s_inactive_parent",
        text="first accepted child input",
    )
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id="t_inactive_parent",
            owner_id="parent-owner",
        )
        == "claimed"
    )
    assert await store.materialize_steer_follow_up(
        session_id="s_inactive_parent",
        current_turn_id="t_inactive_parent",
        next_turn_id="t_active_child",
    ) == ["first accepted child input"]
    assert await store.append_steer_buffer(
        session_id="s_inactive_parent",
        expected_turn_id="t_active_child",
        text="buffer owned by child",
    )
    assert await store.mark_completed(
        turn_id="t_inactive_parent",
        intent="lookup",
        final_answer="parent result",
        tokens_input=3,
        tokens_output=5,
        cost_usd=0.1,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
            (
                (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat(),
                "t_inactive_parent",
            ),
        )
        child_before = connection.execute(
            "SELECT t.status,d.state,d.owner_id,d.lease_expires_at,"
            "s.active_turn_id,s.steer_buffer_json FROM turns t "
            "JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id "
            "JOIN sessions s ON s.id=t.session_id WHERE t.id='t_active_child'",
        ).fetchone()
        connection.commit()

    assert await store.recover_expired_chat_spawn_dispatches() == [
        "t_inactive_parent",
    ]
    events = await log_store.list_events("t_inactive_parent")
    assert [event.event_type for event in events] == ["final_answer", "done"]
    assert events[0].payload["text"] == "parent result"
    assert events[1].payload["status"] == "completed"
    with sqlite3.connect(db_path) as connection:
        child_after = connection.execute(
            "SELECT t.status,d.state,d.owner_id,d.lease_expires_at,"
            "s.active_turn_id,s.steer_buffer_json FROM turns t "
            "JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id "
            "JOIN sessions s ON s.id=t.session_id WHERE t.id='t_active_child'",
        ).fetchone()
        assert connection.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id='t_inactive_parent'",
        ).fetchone() == ("failed",)
    assert child_after == child_before
    assert json.loads(child_after[-1])[0]["text"] == "buffer owned by child"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_status", "error_kind", "preexisting_final_answer"),
    [
        ("completed", None, False),
        ("completed", None, True),
        ("failed", "provider_secret_kind", False),
        ("error", "internal", False),
        ("cancelled", "user_cancel", False),
        ("user_cancelled", "user_cancel", False),
        ("aborted", "aborted", False),
        ("clarification_pending", "needs_clarification", False),
        ("awaiting_user", "needs_user_input", False),
        ("awaiting_approval", "awaiting_approval", False),
    ],
)
async def test_expired_dispatch_reconstructs_missing_done_from_terminal_turn(
    db_path: str,
    terminal_status: str,
    error_kind: str | None,
    preexisting_final_answer: bool,
) -> None:
    store = SessionStore(db_path)
    log_store = TurnLogStore(db_path)
    await log_store.initialize()
    suffix = f"{terminal_status}_{int(preexisting_final_answer)}"
    turn_id = f"t_missing_done_{suffix}"
    session_id = f"s_missing_done_{suffix}"
    await store.ensure_session(
        session_id=session_id,
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id=turn_id,
        session_id=session_id,
        user_text="terminal Store fact",
    )
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id=turn_id,
            owner_id="terminal-owner",
        )
        == "claimed"
    )
    ended_at = datetime.now(tz=UTC).isoformat()
    expired = (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turns SET status=?,ended_at=?,intent='lookup',"
            "final_answer='persisted answer',tokens_input=7,tokens_output=11,"
            "cost_usd=0.25,error=?,error_kind=? WHERE id=?",
            (
                terminal_status,
                ended_at,
                ("postgres://admin:sentinel-secret@db.internal/query" if terminal_status == "failed" else None),
                error_kind,
                turn_id,
            ),
        )
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
            (expired, turn_id),
        )
        if preexisting_final_answer:
            connection.execute(
                "INSERT INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES (?,?,1,'final_answer',?,?)",
                (
                    f"evt_existing_final_{turn_id}",
                    turn_id,
                    json.dumps(
                        {
                            "text": "persisted answer",
                            "card_id": turn_id,
                            "seq": 1,
                            "evt_seq": 1,
                        },
                    ),
                    ended_at,
                ),
            )
        connection.commit()
    before = await store.get_turn(turn_id)
    assert before is not None

    assert await store.recover_expired_chat_spawn_dispatches() == [turn_id]
    assert await store.get_turn(turn_id) == before
    events = await log_store.list_events(turn_id)
    assert events[-1].event_type == "done"
    wire_status = {
        "error": "failed",
        "user_cancelled": "cancelled",
        "awaiting_user": "clarification_pending",
        "awaiting_approval": "approval_pending",
    }.get(terminal_status, terminal_status)
    expected_error_kind = "internal" if terminal_status in {"failed", "error"} else (None if terminal_status == "awaiting_approval" else error_kind)
    expected_done = {
        "turn_id": turn_id,
        "status": wire_status,
        "schema_version": "0.9.0",
        "tokens_input": 7,
        "tokens_output": 11,
        "cost_usd": 0.25,
        "seq": events[-1].seq,
        "evt_seq": events[-1].payload["evt_seq"],
        "intent": "lookup",
        **({"error": "internal_error"} if terminal_status in {"failed", "error"} else {}),
        **({"error_kind": expected_error_kind} if expected_error_kind else {}),
        # Chat turn feedback D5: the reconstructed failed done names its public reason and action.
        **({"reason_code": "lead_internal_error", "recovery_action": "retry"} if terminal_status in {"failed", "error"} else {}),
    }
    assert events[-1].payload == expected_done
    assert [event.event_type for event in events if event.event_type == "final_answer"] == [
        "final_answer",
    ]
    assert next(event.payload["text"] for event in events if event.event_type == "final_answer") == "persisted answer"
    assert "sentinel-secret" not in json.dumps(
        [event.payload for event in events],
    )
    assert await store.recover_expired_chat_spawn_dispatches() == []
    assert [event.event_type for event in await log_store.list_events(turn_id)].count(
        "done",
    ) == 1


@pytest.mark.asyncio
async def test_expired_dispatch_repair_continues_after_legacy_row_cursor(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    log_store = TurnLogStore(db_path)
    await log_store.initialize()
    turn_id = "t_legacy_cursor_recovery"
    session_id = "s_legacy_cursor_recovery"
    await store.ensure_session(
        session_id=session_id,
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id=turn_id,
        session_id=session_id,
        user_text="legacy events have no evt_seq",
    )
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id=turn_id,
            owner_id="expired-owner",
        )
        == "claimed"
    )
    ended_at = datetime.now(tz=UTC).isoformat()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turns SET status='completed',ended_at=?,final_answer='' WHERE id=?",
            (ended_at, turn_id),
        )
        for row_seq, payload_seq in ((1, 1), (2, 3)):
            connection.execute(
                "INSERT INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES (?,?,?,'narrative.delta',?,?)",
                (
                    f"evt_legacy_cursor_{row_seq}",
                    turn_id,
                    row_seq,
                    json.dumps(
                        {
                            "seq": payload_seq,
                            "text": str(payload_seq),
                        }
                    ),
                    ended_at,
                ),
            )
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
            (
                (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat(),
                turn_id,
            ),
        )
        connection.commit()

    assert await store.recover_expired_chat_spawn_dispatches() == [turn_id]
    events = await log_store.list_events(turn_id)
    assert events[-1].seq == 3
    assert events[-1].event_type == "done"
    assert events[-1].payload["seq"] == 4
    assert events[-1].payload["evt_seq"] == 4


@pytest.mark.asyncio
async def test_matching_done_without_canonical_final_answer_rebuilds_ordered_tail(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    log_store = TurnLogStore(db_path)
    await log_store.initialize()
    turn_id = "t_matching_done_missing_final"
    session_id = "s_matching_done_missing_final"
    await store.ensure_session(
        session_id=session_id,
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id=turn_id,
        session_id=session_id,
        user_text="answer must precede terminal",
    )
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id=turn_id,
            owner_id="expired-owner",
        )
        == "claimed"
    )
    assert await store.mark_completed(
        turn_id=turn_id,
        intent="lookup",
        final_answer="canonical answer",
        tokens_input=1,
        tokens_output=1,
        cost_usd=0.0,
    )
    assert await log_store.record(
        turn_id=turn_id,
        event_type="done",
        payload={"turn_id": turn_id, "status": "completed"},
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
            (
                (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat(),
                turn_id,
            ),
        )
        connection.commit()

    assert await store.recover_expired_chat_spawn_dispatches() == [turn_id]
    events = await log_store.list_events(turn_id)
    assert [event.event_type for event in events] == [
        "done",
        "final_answer",
        "done",
    ]


@pytest.mark.asyncio
async def test_matching_final_answer_after_done_requires_fresh_terminal_tail(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    log_store = TurnLogStore(db_path)
    await log_store.initialize()
    turn_id = "t_answer_after_done"
    session_id = "s_answer_after_done"
    await store.ensure_session(
        session_id=session_id,
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id=turn_id,
        session_id=session_id,
        user_text="terminal must follow its answer",
    )
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id=turn_id,
            owner_id="expired-owner",
        )
        == "claimed"
    )
    assert await store.mark_completed(
        turn_id=turn_id,
        intent="lookup",
        final_answer="canonical answer",
        tokens_input=1,
        tokens_output=1,
        cost_usd=0.0,
    )
    assert await log_store.record(
        turn_id=turn_id,
        event_type="done",
        payload={"turn_id": turn_id, "status": "completed"},
    )
    assert await log_store.record(
        turn_id=turn_id,
        event_type="final_answer",
        payload={"text": "canonical answer", "card_id": turn_id},
    )

    assert not await store.mark_chat_spawn_dispatch_terminal(
        turn_id=turn_id,
        owner_id="expired-owner",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
            (
                (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat(),
                turn_id,
            ),
        )
        connection.commit()

    assert await store.recover_expired_chat_spawn_dispatches() == [turn_id]
    events = await log_store.list_events(turn_id)
    assert [event.event_type for event in events] == [
        "done",
        "final_answer",
        "done",
    ]
    assert events[-1].payload["status"] == "completed"
    assert events[-2].payload["text"] == "canonical answer"
    assert events[-1].payload["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("canonical_status", "stale_wire_status", "expected_status"),
    [
        ("running", "completed", "failed"),
        ("completed", "error", "completed"),
    ],
)
async def test_expired_dispatch_corrects_stale_terminal_to_canonical_turn_status(
    db_path: str,
    canonical_status: str,
    stale_wire_status: str,
    expected_status: str,
) -> None:
    store = SessionStore(db_path)
    log_store = TurnLogStore(db_path)
    await log_store.initialize()
    suffix = f"{canonical_status}_{stale_wire_status}"
    turn_id = f"t_stale_terminal_{suffix}"
    session_id = f"s_stale_terminal_{suffix}"
    await store.ensure_session(
        session_id=session_id,
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id=turn_id,
        session_id=session_id,
        user_text="canonical Store status wins",
    )
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id=turn_id,
            owner_id="expired-owner",
        )
        == "claimed"
    )
    if canonical_status == "completed":
        assert await store.mark_completed(
            turn_id=turn_id,
            intent="lookup",
            final_answer="canonical answer",
            tokens_input=1,
            tokens_output=2,
            cost_usd=0.01,
        )
    stale = await log_store.record(
        turn_id=turn_id,
        event_type="done",
        payload={
            "turn_id": turn_id,
            "status": stale_wire_status,
            "seq": 1,
            "evt_seq": 1,
        },
    )
    assert stale is not None
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
            (
                (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat(),
                turn_id,
            ),
        )
        connection.commit()

    assert await store.recover_expired_chat_spawn_dispatches() == [turn_id]
    turn = await store.get_turn(turn_id)
    assert turn is not None
    assert turn.status == expected_status
    events = await log_store.list_events(turn_id)
    assert [event.payload.get("status") for event in events if event.event_type == "done"] == [stale_wire_status, expected_status]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == ("failed",)


@pytest.mark.asyncio
async def test_dispatch_settlement_requires_matching_canonical_root_terminal(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    session_id = "s_dispatch_terminal_proof"
    turn_id = "t_dispatch_terminal_proof"
    await store.ensure_session(
        session_id=session_id,
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id=turn_id,
        session_id=session_id,
        user_text="stop wins",
    )
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id=turn_id,
            owner_id="cancelled-owner",
        )
        == "claimed"
    )
    assert await store.mark_cancelled(
        turn_id=turn_id,
        error_kind="user_cancel",
    )

    assert not await store.mark_chat_spawn_dispatch_terminal(
        turn_id=turn_id,
        owner_id="cancelled-owner",
    )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == ("spawned",)

    await _record_root_done(
        db_path,
        turn_id=turn_id,
        status="cancelled",
    )
    assert await store.mark_chat_spawn_dispatch_terminal(
        turn_id=turn_id,
        owner_id="cancelled-owner",
    )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == ("terminal",)


@pytest.mark.asyncio
async def test_stale_terminal_repair_rolls_back_before_canonical_done(
    db_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(db_path)
    log_store = TurnLogStore(db_path)
    await log_store.initialize()
    turn_id = "t_stale_terminal_rollback"
    session_id = "s_stale_terminal_rollback"
    await store.ensure_session(
        session_id=session_id,
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id=turn_id,
        session_id=session_id,
        user_text="repair must be atomic",
    )
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id=turn_id,
            owner_id="expired-owner",
        )
        == "claimed"
    )
    assert await log_store.record(
        turn_id=turn_id,
        event_type="done",
        payload={
            "turn_id": turn_id,
            "status": "completed",
            "seq": 1,
            "evt_seq": 1,
        },
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
            (
                (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat(),
                turn_id,
            ),
        )
        connection.commit()
        dispatch_state_before = connection.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (turn_id,),
        ).fetchone()

    original_execute = aiosqlite.Connection.execute

    def _fail_canonical_done(self, sql, parameters=None):
        normalized = " ".join(str(sql).split())
        if normalized.startswith("INSERT INTO turn_events") and "'done'" in normalized:
            raise sqlite3.OperationalError("injected canonical done failure")
        return original_execute(self, sql, parameters)

    monkeypatch.setattr(aiosqlite.Connection, "execute", _fail_canonical_done)
    with pytest.raises(
        sqlite3.OperationalError,
        match="injected canonical done failure",
    ):
        await store.recover_expired_chat_spawn_dispatches()

    turn = await store.get_turn(turn_id)
    assert turn is not None
    assert turn.status == "running"
    assert await store.get_active_turn(session_id) == turn_id
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id=?",
                (turn_id,),
            ).fetchone()
            == dispatch_state_before
        )
        remaining_events = connection.execute(
            "SELECT event_type,payload_json FROM turn_events WHERE turn_id=? ORDER BY seq",
            (turn_id,),
        ).fetchall()
        assert len(remaining_events) == 1
        assert remaining_events[0][0] == "done"
        assert json.loads(remaining_events[0][1]) == {
            "turn_id": turn_id,
            "status": "completed",
            "seq": 1,
            "evt_seq": 1,
        }


@pytest.mark.asyncio
async def test_expired_dispatch_materializes_accepted_steer_before_clearing_buffer(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    start = {
        "team_id": "team_1",
        "user_id": "u_1",
        "client_submission_id": "018f0cc3-e4a2-7b21-a109-001122334519",
        "workspace_id": "ws_1",
        "requested_session_id": None,
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "request_fingerprint": "9" * 64,
        "accepted_request_fingerprints": ("9" * 64,),
        "fingerprint_key_id": "f" * 64,
        "auth_session_generation_hash": "0" * 64,
    }
    start_claim = await store.claim_chat_submission(**start)
    await store.finalize_started_chat_submission(
        **_finalize_args(start),
        actual_session_id="s_steer_owner_lost",
        turn_id="t_steer_owner_lost",
        user_text="long task",
        attachment_ids=[],
        lease_owner=start_claim.lease_owner,
        lease_version=start_claim.lease_version,
        response={"status": "started"},
    )
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id="t_steer_owner_lost",
            owner_id="owner-a",
        )
        == "claimed"
    )

    steer = {
        **start,
        "client_submission_id": "018f0cc3-e4a2-7b21-a109-001122334520",
        "requested_session_id": "s_steer_owner_lost",
        "submission_kind": "steer",
        "request_fingerprint": "a" * 64,
        "accepted_request_fingerprints": ("a" * 64,),
    }
    steer_claim = await store.claim_chat_submission(**steer)
    steer_receipt = await store.finalize_steer_chat_submission(
        **{key: value for key, value in _finalize_args(steer).items() if key != "submission_kind"},
        expected_parent_turn_id="t_steer_owner_lost",
        expected_execution_generation=1,
        text="also compare the west region",
        attachment_ids=[],
        lease_owner=steer_claim.lease_owner,
        lease_version=steer_claim.lease_version,
    )
    assert steer_receipt.outcome == "buffered"

    suffix = [
        {
            "text": "@Analyst /method queued reference",
            "submission_id": "reference-after-owner-loss",
            "reference_submission_id": "reference-after-owner-loss",
        },
        {"text": "plain after reference", "submission_id": "plain-after-reference"},
    ]

    with sqlite3.connect(db_path) as connection:
        buffer = json.loads(connection.execute("SELECT steer_buffer_json FROM sessions WHERE id=?", ("s_steer_owner_lost",)).fetchone()[0])
        connection.execute(
            "UPDATE sessions SET steer_buffer_json=? WHERE id=?",
            (json.dumps([*buffer, *suffix], ensure_ascii=False), "s_steer_owner_lost"),
        )
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
            (
                (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat(),
                "t_steer_owner_lost",
            ),
        )
        connection.commit()

    assert await store.recover_expired_chat_spawn_dispatches() == ["t_steer_owner_lost"]
    follow_up = await store.get_chat_follow_up(
        parent_turn_id="t_steer_owner_lost",
    )
    assert follow_up is not None
    child_turn_id = ""
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        child = connection.execute(
            "SELECT t.id,t.user_text,t.status,d.state,u.parts_json "
            "FROM turns t JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id "
            "JOIN chat_turn_user_sources u ON u.turn_id=t.id "
            "WHERE d.dispatch_kind='follow_up' AND t.session_id=?",
            ("s_steer_owner_lost",),
        ).fetchone()
        assert child is not None
        child_turn_id = str(child["id"])
        assert follow_up == (child["id"], 1)
        assert child["user_text"] == "also compare the west region"
        assert child["status"] == "failed"
        assert child["state"] == "failed"
        assert json.loads(child["parts_json"]) == [{"text": "also compare the west region", "attachments": []}]
        active_turn_id, buffer_json = connection.execute(
            "SELECT active_turn_id,steer_buffer_json FROM sessions WHERE id=?",
            ("s_steer_owner_lost",),
        ).fetchone()
        assert active_turn_id == child_turn_id and json.loads(buffer_json) == suffix
    child_events = await TurnLogStore(db_path).list_events(child_turn_id)
    assert [event.event_type for event in child_events] == ["turn_failed", "done"]
    assert child_events[0].payload["reason_code"] == "lead_internal_error"
    assert child_events[-1].payload["status"] == "failed"
    assert child_events[-1].payload["error_kind"] == "steer_recovery_required"


@pytest.mark.asyncio
async def test_recent_submission_epochs_must_be_loaded_by_process_keyring(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    common = {
        "team_id": "team_1",
        "user_id": "u_1",
        "client_submission_id": "018f0cc3-e4a2-7b21-a109-001122334521",
        "workspace_id": "ws_1",
        "requested_session_id": None,
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "request_fingerprint": "b" * 64,
        "accepted_request_fingerprints": ("b" * 64,),
        "fingerprint_key_id": "required_epoch".ljust(64, "0"),
        "auth_session_generation_hash": "0" * 64,
    }
    claim = await store.claim_chat_submission(**common)
    assert claim.kind == "claimed"

    assert not await store.chat_submission_fingerprint_keyring_aligned(
        configured_key_ids=("current_only".ljust(64, "0"),),
    )
    assert await store.chat_submission_fingerprint_keyring_aligned(
        configured_key_ids=(common["fingerprint_key_id"],),
    )


@pytest.mark.asyncio
async def test_key_epoch_rotation_rejects_reverse_and_deadline_extension(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    old_key = "old_epoch".ljust(64, "0")
    new_key = "new_epoch".ljust(64, "0")
    deadline = datetime.now(tz=UTC) + timedelta(days=7)

    assert await store.chat_submission_fingerprint_keyring_aligned(
        configured_key_ids=(old_key,),
    )
    assert await store.chat_submission_fingerprint_keyring_aligned(
        configured_key_ids=(new_key, old_key),
        previous_accept_until=deadline.isoformat(),
    )
    assert not await store.chat_submission_fingerprint_keyring_aligned(
        configured_key_ids=(old_key, new_key),
        previous_accept_until=deadline.isoformat(),
    )
    assert not await store.chat_submission_fingerprint_keyring_aligned(
        configured_key_ids=(new_key, old_key),
        previous_accept_until=(deadline + timedelta(minutes=1)).isoformat(),
    )
    assert await store.chat_submission_fingerprint_keyring_aligned(
        configured_key_ids=(new_key, old_key),
        previous_accept_until=deadline.isoformat(),
    )


@pytest.mark.asyncio
async def test_unknown_previous_key_cannot_be_added_to_initialized_epoch_truth(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    current_key = "current_epoch".ljust(64, "0")
    unknown_key = "unknown_epoch".ljust(64, "0")
    assert await store.chat_submission_fingerprint_keyring_aligned(
        configured_key_ids=(current_key,),
    )

    assert not await store.chat_submission_fingerprint_keyring_aligned(
        configured_key_ids=(current_key, unknown_key),
        previous_accept_until=(datetime.now(tz=UTC) + timedelta(days=7)).isoformat(),
    )


@pytest.mark.asyncio
async def test_expired_orphan_claim_converges_to_abandon_tombstone(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    old_key = "orphan_old".ljust(64, "0")
    new_key = "orphan_new".ljust(64, "0")
    common = {
        "team_id": "team_1",
        "user_id": "u_1",
        "client_submission_id": "018f0cc3-e4a2-7b21-a109-001122334522",
        "workspace_id": "ws_1",
        "requested_session_id": None,
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "request_fingerprint": "c" * 64,
        "accepted_request_fingerprints": ("c" * 64,),
        "fingerprint_key_id": old_key,
        "auth_session_generation_hash": "0" * 64,
        "lease_seconds": -1,
    }
    claim = await store.claim_chat_submission(**common)
    assert claim.kind == "claimed"

    boundary = datetime.now(tz=UTC).isoformat()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE security_key_epochs SET retired_at=?,accept_until=? WHERE purpose='chat_submission_fingerprint' AND key_id=?",
            (boundary, boundary, old_key),
        )
        connection.execute(
            "INSERT INTO security_key_epochs(purpose,key_id,activated_at,retired_at,accept_until) "
            "VALUES ('chat_submission_fingerprint',?,?,NULL,NULL)",
            (new_key, boundary),
        )
        connection.commit()

    assert await store.chat_submission_fingerprint_keyring_aligned(
        configured_key_ids=(new_key,),
    )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_submission_inflight WHERE client_submission_id=?",
            (common["client_submission_id"],),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_submission_abandon_tombstones WHERE client_submission_id=?",
            (common["client_submission_id"],),
        ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_invalidated_auth_generation_cannot_finalize_claim(db_path: str) -> None:
    store = SessionStore(db_path)
    common = {
        "team_id": "team_1",
        "user_id": "u_1",
        "client_submission_id": "018f0cc3-e4a2-7b21-a109-001122334518",
        "workspace_id": "ws_1",
        "requested_session_id": None,
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "request_fingerprint": "a" * 64,
        "accepted_request_fingerprints": ("a" * 64,),
        "fingerprint_key_id": "f" * 64,
        "auth_session_generation_hash": "1" * 64,
    }
    claim = await store.claim_chat_submission(**common)
    assert claim.kind == "claimed"
    assert await store.invalidate_chat_auth_session_generation(
        user_id="u_1",
        auth_session_generation_hash="1" * 64,
    )

    with pytest.raises(ValueError, match="submission_auth_generation_inactive"):
        await store.finalize_started_chat_submission(
            **_finalize_args(common),
            actual_session_id="s_auth_fenced",
            turn_id="t_auth_fenced",
            user_text="must not commit",
            attachment_ids=[],
            lease_owner=claim.lease_owner,
            lease_version=claim.lease_version,
            response={"status": "started"},
        )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM turns WHERE id='t_auth_fenced'",
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_previous_fingerprint_epoch_replays_existing_receipt(db_path: str) -> None:
    store = SessionStore(db_path)
    common = {
        "team_id": "team_1",
        "user_id": "u_1",
        "client_submission_id": "018f0cc3-e4a2-7b21-a109-001122334519",
        "workspace_id": "ws_1",
        "requested_session_id": None,
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "request_fingerprint": "a" * 64,
        "accepted_request_fingerprints": ("a" * 64,),
        "fingerprint_key_id": "e" * 64,
        "auth_session_generation_hash": "2" * 64,
    }
    claim = await store.claim_chat_submission(**common)
    await store.finalize_started_chat_submission(
        **_finalize_args(common),
        actual_session_id="s_key_epoch",
        turn_id="t_key_epoch",
        user_text="same semantic wire",
        attachment_ids=[],
        lease_owner=claim.lease_owner,
        lease_version=claim.lease_version,
        response={"status": "started"},
    )
    assert await store.chat_submission_fingerprint_keyring_aligned(
        configured_key_ids=("f" * 64, "e" * 64),
        previous_accept_until=(datetime.now(tz=UTC) + timedelta(days=7)).isoformat(),
    )

    replay = await store.claim_chat_submission(
        **{
            **common,
            "request_fingerprint": "b" * 64,
            "accepted_request_fingerprints": ("b" * 64, "a" * 64),
            "fingerprint_key_id": "f" * 64,
        },
    )

    assert replay.kind == "receipt"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT fingerprint_key_id FROM chat_submission_receipts WHERE client_submission_id=?",
            (common["client_submission_id"],),
        ).fetchone() == ("e" * 64,)


@pytest.mark.asyncio
async def test_cancel_receipt_is_idempotent_and_generation_fenced(db_path: str) -> None:
    store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_cancel_idem",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_cancel_idem",
        session_id="s_cancel_idem",
        user_text="run",
    )
    common = {
        "team_id": "team_1",
        "user_id": "u_1",
        "cancel_request_id": "018f0cc3-e4a2-4b21-a109-001122334592",
        "workspace_id": "ws_1",
        "turn_id": "t_cancel_idem",
        "expected_execution_generation": 1,
    }

    assert await store.request_cancel_turn(**common) == "cancelled"
    assert await store.request_cancel_turn(**common) == "cancelled"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM turn_cancel_receipts WHERE cancel_request_id=?",
            (common["cancel_request_id"],),
        ).fetchone() == (1,)

    await store.ensure_session(
        session_id="s_cancel_gen",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_cancel_gen",
        session_id="s_cancel_gen",
        user_text="run",
    )
    conflict = await store.request_cancel_turn(
        **{
            **common,
            "cancel_request_id": "018f0cc3-e4a2-4b21-a109-001122334593",
            "turn_id": "t_cancel_gen",
            "expected_execution_generation": 2,
        },
    )
    assert conflict == "generation_conflict"
    turn = await store.get_turn("t_cancel_gen")
    assert turn is not None and turn.status == "running"


@pytest.mark.asyncio
async def test_stop_atomically_cancels_bound_plan_with_turn(db_path: str) -> None:
    session_store = SessionStore(db_path)
    plan_store = PlanStore(db_path)
    await session_store.ensure_session(
        session_id="s_plan_stop",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await session_store.record_turn(
        turn_id="t_plan_stop",
        session_id="s_plan_stop",
        user_text="execute plan",
    )
    assert (
        await session_store.claim_chat_spawn_dispatch_for_worker(
            turn_id="t_plan_stop",
            owner_id="gateway-auto-owner",
        )
        == "claimed"
    )
    plan = await plan_store.create_plan(
        session_id="s_plan_stop",
        workspace_id="ws_1",
        user_id="u_1",
        title="atomic stop",
    )
    assert await session_store.try_claim_auto_plan_for_live_turn(
        plan_id=plan.id,
        turn_id="t_plan_stop",
    )

    outcome = await session_store.request_cancel_turn(
        team_id="team_1",
        user_id="u_1",
        cancel_request_id="018f0cc3-e4a2-4b21-a109-001122334591",
        workspace_id="ws_1",
        turn_id="t_plan_stop",
        expected_execution_generation=1,
        include_plan_outcome=True,
    )

    assert outcome == ("cancelled", True)
    turn = await session_store.get_turn("t_plan_stop")
    got_plan = await plan_store.get_plan(
        plan.id,
        user_id="u_1",
        workspace_id="ws_1",
    )
    assert turn is not None and turn.status == "cancelled"
    assert got_plan is not None and got_plan.status == PlanStatus.CANCELLED

    # Same idempotency key repairs a legacy split-transaction residue instead
    # of replaying success while leaving the Plan RUNNING forever.
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE plans SET status='running' WHERE id=?",
            (plan.id,),
        )
        connection.commit()
    replay = await session_store.request_cancel_turn(
        team_id="team_1",
        user_id="u_1",
        cancel_request_id="018f0cc3-e4a2-4b21-a109-001122334591",
        workspace_id="ws_1",
        turn_id="t_plan_stop",
        expected_execution_generation=1,
        include_plan_outcome=True,
    )
    assert replay == ("cancelled", True)
    repaired = await plan_store.get_plan(
        plan.id,
        user_id="u_1",
        workspace_id="ws_1",
    )
    assert repaired is not None and repaired.status == PlanStatus.CANCELLED


@pytest.mark.asyncio
async def test_stop_before_auto_plan_claim_rejects_stale_worker(
    db_path: str,
) -> None:
    worker_store = SessionStore(db_path)
    stop_store = SessionStore(db_path)
    plan_store = PlanStore(db_path)
    await worker_store.ensure_session(
        session_id="s_auto_stop_first",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await worker_store.record_turn(
        turn_id="t_auto_stop_first",
        session_id="s_auto_stop_first",
        user_text="auto plan",
    )
    assert (
        await worker_store.claim_chat_spawn_dispatch_for_worker(
            turn_id="t_auto_stop_first",
            owner_id="gateway-old-worker",
        )
        == "claimed"
    )
    plan = await plan_store.create_plan(
        session_id="s_auto_stop_first",
        workspace_id="ws_1",
        user_id="u_1",
        title="stop wins",
    )

    assert await stop_store.request_cancel_turn(
        team_id="team_1",
        user_id="u_1",
        cancel_request_id="018f0cc3-e4a2-4b21-a109-001122334590",
        workspace_id="ws_1",
        turn_id="t_auto_stop_first",
        expected_execution_generation=1,
        include_plan_outcome=True,
    ) == ("cancelled", False)
    assert not await worker_store.try_claim_auto_plan_for_live_turn(
        plan_id=plan.id,
        turn_id="t_auto_stop_first",
    )
    current = await plan_store.get_plan(
        plan.id,
        user_id="u_1",
        workspace_id="ws_1",
    )
    assert current is not None
    assert current.status == PlanStatus.PROPOSED
    assert current.execution_turn_id is None


@pytest.mark.asyncio
async def test_auto_plan_claim_rejects_plan_from_another_session(
    db_path: str,
) -> None:
    session_store = SessionStore(db_path)
    plan_store = PlanStore(db_path)
    await session_store.ensure_session(
        session_id="s_auto_live",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await session_store.ensure_session(
        session_id="s_auto_foreign_plan",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await session_store.record_turn(
        turn_id="t_auto_live",
        session_id="s_auto_live",
        user_text="auto plan",
    )
    assert (
        await session_store.claim_chat_spawn_dispatch_for_worker(
            turn_id="t_auto_live",
            owner_id="gateway-auto-live",
        )
        == "claimed"
    )
    foreign_plan = await plan_store.create_plan(
        session_id="s_auto_foreign_plan",
        workspace_id="ws_1",
        user_id="u_1",
        title="foreign session plan",
    )

    assert not await session_store.try_claim_auto_plan_for_live_turn(
        plan_id=foreign_plan.id,
        turn_id="t_auto_live",
    )
    current = await plan_store.get_plan(
        foreign_plan.id,
        user_id="u_1",
        workspace_id="ws_1",
    )
    assert current is not None
    assert current.status == PlanStatus.PROPOSED
    assert current.execution_turn_id is None


@pytest.mark.asyncio
async def test_stop_preserves_acknowledged_steer_until_follow_up_materializes(
    db_path: str,
) -> None:
    store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_cancel_with_steer",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_cancel_with_steer",
        session_id="s_cancel_with_steer",
        user_text="parent",
    )
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id="t_cancel_with_steer",
            owner_id="gateway-owner",
        )
        == "claimed"
    )
    common = {
        "team_id": "team_1",
        "user_id": "u_1",
        "client_submission_id": "018f0cc3-e4a2-7b21-a109-001122334599",
        "workspace_id": "ws_1",
        "requested_session_id": "s_cancel_with_steer",
        "submission_kind": "steer",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "request_fingerprint": "c" * 64,
        "accepted_request_fingerprints": ("c" * 64,),
        "fingerprint_key_id": "f" * 64,
        "auth_session_generation_hash": "0" * 64,
    }
    claim = await store.claim_chat_submission(**common)
    receipt = await store.finalize_steer_chat_submission(
        **{key: value for key, value in _finalize_args(common).items() if key != "submission_kind"},
        expected_parent_turn_id="t_cancel_with_steer",
        expected_execution_generation=1,
        text="keep this follow-up",
        attachment_ids=[],
        lease_owner=claim.lease_owner,
        lease_version=claim.lease_version,
    )
    assert receipt.outcome == "buffered"

    assert (
        await store.request_cancel_turn(
            team_id="team_1",
            user_id="u_1",
            cancel_request_id="018f0cc3-e4a2-4b21-a109-001122334598",
            workspace_id="ws_1",
            turn_id="t_cancel_with_steer",
            expected_execution_generation=1,
        )
        == "cancelled"
    )
    assert await store.get_active_turn("s_cancel_with_steer") == "t_cancel_with_steer"
    assert await store.materialize_steer_follow_up(
        session_id="s_cancel_with_steer",
        current_turn_id="t_cancel_with_steer",
        next_turn_id="t_cancel_with_steer_child",
    ) == ["keep this follow-up"]
    source = await store.get_chat_turn_user_source(
        turn_id="t_cancel_with_steer_child",
        user_id="u_1",
        workspace_id="ws_1",
    )
    assert source is not None
    assert source.user_text == "keep this follow-up"


@pytest.mark.asyncio
async def test_cancel_receipt_survives_turn_erasure_and_expires(db_path: str) -> None:
    store = SessionStore(db_path)
    await store.ensure_session(
        session_id="s_cancel_erased",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await store.record_turn(
        turn_id="t_cancel_erased",
        session_id="s_cancel_erased",
        user_text="run",
    )
    common = {
        "team_id": "team_1",
        "user_id": "u_1",
        "cancel_request_id": "018f0cc3-e4a2-4b21-a109-001122334594",
        "workspace_id": "ws_1",
        "turn_id": "t_cancel_erased",
        "expected_execution_generation": 1,
    }
    assert await store.request_cancel_turn(**common) == "cancelled"

    with sqlite3.connect(db_path) as connection:
        connection.execute("DELETE FROM turns WHERE id='t_cancel_erased'")
        connection.commit()
        assert connection.execute(
            "SELECT turn_erased FROM turn_cancel_receipts WHERE cancel_request_id=?",
            (common["cancel_request_id"],),
        ).fetchone() == (1,)

    receipt = await store.get_cancel_turn_receipt(
        team_id="team_1",
        user_id="u_1",
        cancel_request_id=str(common["cancel_request_id"]),
        turn_id="t_cancel_erased",
        expected_execution_generation=1,
    )
    assert receipt is not None
    assert receipt.outcome == "cancelled"
    assert receipt.turn_erased is True

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE turn_cancel_receipts SET expires_at=? WHERE cancel_request_id=?",
            (
                (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat(),
                common["cancel_request_id"],
            ),
        )
        connection.commit()
    assert await store.cleanup_chat_submission_protocol() == 1
    assert (
        await store.get_cancel_turn_receipt(
            team_id="team_1",
            user_id="u_1",
            cancel_request_id=str(common["cancel_request_id"]),
            turn_id="t_cancel_erased",
            expected_execution_generation=1,
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["start", "replay", "steer"])
@pytest.mark.parametrize("relabel_as_legacy", [False, True])
async def test_reference_identity_can_recover_rejection_but_cannot_use_plaintext_materializer(
    db_path: str, kind: str, relabel_as_legacy: bool
) -> None:
    store = SessionStore(db_path)
    common = {
        "team_id": "team_1",
        "user_id": "u_1",
        "client_submission_id": "0da31a6b-2c6e-4fb3-8c80-f034b37d840f",
        "workspace_id": "ws_1",
        "requested_session_id": "s_reference",
        "submission_kind": kind,
        "submission_contract_version": "chat-references.v1",
        "model_control_batch": "references",
        "request_fingerprint": "1" * 64,
        "accepted_request_fingerprints": ("1" * 64,),
        "fingerprint_key_id": "f" * 64,
        "auth_session_generation_hash": "0" * 64,
    }
    claim = await store.claim_chat_submission(**common)
    assert claim.kind == "claimed"
    args = {**_finalize_args(common), "lease_owner": claim.lease_owner, "lease_version": claim.lease_version}
    if relabel_as_legacy:
        args.update(submission_contract_version="chat-submit.v1", model_control_batch="a")
    with pytest.raises(ValueError, match="reference_submission_materializer_unavailable"):
        if kind == "steer":
            steer_args = {key: value for key, value in args.items() if key != "submission_kind"}
            await store.finalize_steer_chat_submission(
                **steer_args, expected_parent_turn_id="t_parent", expected_execution_generation=1, text="/杜邦分析 盈利分析", attachment_ids=[]
            )
        else:
            await store.finalize_started_chat_submission(
                **args,
                actual_session_id="s_reference",
                turn_id="t_ref",
                user_text="/杜邦分析 盈利分析",
                attachment_ids=[],
                response={"session_id": "s_reference", "turn_id": "t_ref"},
            )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM chat_submission_receipts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM chat_submission_inflight").fetchone()[0] == 1
    args.update(submission_contract_version="chat-references.v1", model_control_batch="references")
    rejection = {"detail": {"error": {"code": "unsupported_entry"}}}
    receipt = await store.finalize_rejected_chat_submission(**args, response=rejection)
    assert receipt.outcome == "rejected"
    assert receipt.response == rejection
    retry = await store.claim_chat_submission(**common)
    assert retry.kind == "receipt"
    assert retry.receipt.response == rejection
