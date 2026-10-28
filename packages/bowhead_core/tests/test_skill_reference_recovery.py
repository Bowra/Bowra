"""Accepted mixed references can settle an expired reservation without dispatch."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import anyio
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_preparation import prepare_reference_team_start_admission
from bowhead_core.chat_reference_runtime_store import is_reference_recoverable_in_connection, is_reference_runtime_in_connection
from bowhead_core.chat_references import AgentResourceRef, InputDocument, ReferenceNode
from bowhead_core.session_lifecycle import SessionLifecycleCoordinator
from bowhead_core.terminal_protocol_store import TerminalProtocolStore
from test_reference_primary_runtime import _expire, _rows
from test_reference_start_preparation import activate
from test_skill_start_admission import candidate
from test_skill_start_admission import receiver_case as _receiver_case
from test_skill_start_admission import start_case as _start_case

receiver_case = _receiver_case
start_case = _start_case
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
]


@pytest.mark.parametrize("agent_first", [False, True])
@pytest.mark.parametrize("lifecycle", ["expired", "revoked", "deleted", "cancelled"])
@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_unclaimed_mixed_input_settles_original_primary_once(receiver_case, agent_first, lifecycle):
    fixture, case = receiver_case, receiver_case.case
    await candidate(fixture, agent_first=agent_first)
    receipt = await case.store.finalize_started_chat_submission(**case.args)
    original_source = _rows(case, "chat_turn_user_sources")
    execution = _rows(case, "turn_agent_execution")[0]
    if lifecycle == "revoked":
        with sqlite3.connect(case.fixture.db_path) as db:
            db.execute("UPDATE agent_assignment SET personal_state='disabled' WHERE agent_id=?", (case.agent.expected.agent_id,))
            db.execute("UPDATE team_members SET status='removed' WHERE user_id=?", (fixture.scope.user_id,))
        await fixture.sources.instances.upsert(
            user_id=fixture.scope.user_id, name="support", source_type="builtin", source_path=str(fixture.root / "support"), enabled=False
        )
    elif lifecycle == "deleted":
        coordinator = SessionLifecycleCoordinator(case.store, TerminalProtocolStore(str(case.fixture.db_path)))
        await coordinator.soft_delete(
            session_id="s_reference", actor_audit_ref=fixture.scope.user_id, request_id="delete-mixed", expected_user_id=fixture.scope.user_id
        )
    elif lifecycle == "cancelled":
        await case.store.request_cancel_turn(
            team_id=fixture.scope.team_id,
            user_id=fixture.scope.user_id,
            workspace_id=fixture.scope.workspace_id,
            turn_id="t_reference",
            expected_execution_generation=1,
            cancel_request_id="cancel-mixed",
        )
    _expire(case)
    assert await case.store.recover_expired_chat_spawn_dispatches() == ["t_reference"]
    assert await case.store.recover_expired_chat_spawn_dispatches() == []
    expected = "cancelled" if lifecycle == "cancelled" else "failed"
    actual = _rows(case, "turn_agent_execution")
    assert len(actual) == 1 and actual[0]["id"] == execution["id"] and actual[0]["status"] == expected
    assert _rows(case, "chat_turn_user_sources") == original_source
    assert (
        _rows(case, "chat_reference_resolutions") == _rows(case, "chat_reference_content_snapshots") == _rows(case, "chat_reference_evidence") == []
    )
    terminals = [json.loads(row["payload_json"]) for row in _rows(case, "turn_events") if row["event_type"] == "done"]
    assert len(terminals) == 1 and terminals[0]["status"] == expected
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute("SELECT status FROM turns WHERE id='t_reference'").fetchone() == (expected,)
        assert db.execute("SELECT active_turn_id FROM sessions WHERE id='s_reference'").fetchone() == (None,)
        saved = json.loads(db.execute("SELECT response_json FROM chat_submission_receipts WHERE turn_id='t_reference'").fetchone()[0])
    assert saved == receipt.response


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("receiver_case", ["admission_capability_skill"], indirect=True)
async def test_unclaimed_skill_capability_combination_is_recoverable(receiver_case, automatic):
    fixture, case = receiver_case, receiver_case.case
    activate(fixture)
    original = case.args["reference_start"].input_document
    agent_node = next(node for node in original.nodes if isinstance(node, ReferenceNode) and isinstance(node.resource, AgentResourceRef))
    nodes = [node.model_dump(mode="python") for node in original.nodes if not automatic or node is not agent_node]
    nodes[:0] = [
        {
            "type": "reference",
            "node_id": str(uuid4()),
            "resource": {"kind": "capability", "id": "cap_visualize", "version": 1},
            "label": "Create visuals",
            "parameters": {},
        },
        {
            "type": "reference",
            "node_id": str(uuid4()),
            "resource": fixture.selected.model_dump(mode="python"),
            "label": "Financial method",
            "parameters": {},
        },
    ]
    document = InputDocument.model_validate({"schema_version": 1, "nodes": nodes})
    prepared = await prepare_reference_team_start_admission(
        case.fixture.db_path,
        input_document=document,
        scope=fixture.scope,
        sources=fixture.sources,
        automatic_receiver=agent_node.resource if automatic else None,
    )
    case.args.update(reference_start=prepared, user_text=document.display_text())
    await case.store.finalize_started_chat_submission(**case.args)
    _expire(case)
    assert await case.store.recover_expired_chat_spawn_dispatches() == ["t_reference"]
    assert await case.store.recover_expired_chat_spawn_dispatches() == []
    assert _rows(case, "turn_agent_execution")[0]["status"] == "failed"
    assert _rows(case, "chat_reference_resolutions") == []


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
@pytest.mark.parametrize("input_kind", ["single", "mixed"])
@pytest.mark.parametrize("other_turn", [False, True])
async def test_failed_terminal_write_preserves_the_reference_reservation_for_retry(receiver_case, input_kind, other_turn):
    case = receiver_case.case
    if input_kind == "mixed":
        await candidate(receiver_case)
    await case.store.finalize_started_chat_submission(**case.args)
    _expire(case)
    if other_turn:
        await case.store.ensure_session(session_id="s_other", workspace_id=case.scope.workspace_id, user_id=case.scope.user_id)
        await case.store.record_turn(turn_id="t_other", session_id="s_other", user_text="Independent task")
        deadline = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        with sqlite3.connect(case.fixture.db_path) as db:
            db.execute(
                "INSERT INTO chat_turn_spawn_dispatch(turn_id,session_id,dispatch_kind,state,lease_expires_at,created_at,updated_at) "
                "VALUES('t_other','s_other','start','claimed',?,?,?)",
                (deadline, deadline, deadline),
            )

    def facts():
        with sqlite3.connect(case.fixture.db_path) as db:
            return {
                table: db.execute(f"SELECT * FROM {table} WHERE {key}=?", (identity,)).fetchall()
                for table, key, identity in (
                    ("sessions", "id", "s_reference"),
                    ("turns", "id", "t_reference"),
                    *[
                        (table, "turn_id", "t_reference")
                        for table in ("turn_agent_execution", "chat_turn_spawn_dispatch", "turn_events", "chat_submission_receipts")
                    ],
                )
            }

    before = facts()
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute(
            "CREATE TRIGGER fail_reference_done BEFORE INSERT ON turn_events WHEN NEW.event_type='done' AND NEW.turn_id='t_reference' "
            "BEGIN SELECT RAISE(ABORT,'injected terminal failure'); END"
        )
    assert await case.store.recover_expired_chat_spawn_dispatches() == (["t_other"] if other_turn else [])
    assert facts() == before
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("DROP TRIGGER fail_reference_done")
    assert await case.store.recover_expired_chat_spawn_dispatches() == ["t_reference"]
    assert _rows(case, "turn_agent_execution")[0]["status"] == "failed"
    assert sum(row["event_type"] == "done" for row in _rows(case, "turn_events")) == 1


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
@pytest.mark.parametrize(
    "fence",
    ["owner", "generation", "resume", "parent", "running", "missing_root", "buffer", "buffer_text_bypass", "buffer_attachment_bypass"],
)
async def test_mixed_recovery_keeps_foreign_and_unsupported_ownership_fenced(receiver_case, fence):
    case = receiver_case.case
    await candidate(receiver_case)
    await case.store.finalize_started_chat_submission(**case.args)
    with sqlite3.connect(case.fixture.db_path) as db:
        statements = {
            "owner": "UPDATE chat_turn_spawn_dispatch SET owner_id='another-owner'",
            "generation": "UPDATE chat_turn_spawn_dispatch SET lease_execution_generation=2",
            "resume": "UPDATE chat_turn_spawn_dispatch SET lease_kind='resume'",
            "parent": "UPDATE chat_turn_spawn_dispatch SET parent_turn_id='another-turn'",
            "running": "UPDATE turn_agent_execution SET status='running',started_at='2026-09-14T00:00:00+00:00'",
            "missing_root": "DELETE FROM turn_agent_binding WHERE turn_id='t_reference'",
            "buffer": "UPDATE sessions SET steer_buffer_json='[{\"user_text\":\"later\"}]' WHERE id='s_reference'",
            "buffer_text_bypass": 'UPDATE sessions SET steer_buffer_json=\'[{"text":"","user_text":"later"}]\' WHERE id=\'s_reference\'',
            "buffer_attachment_bypass": 'UPDATE sessions SET steer_buffer_json=\'[{"attachment_ids":[],"user_text":"later"}]\' WHERE id=\'s_reference\'',
        }
        db.execute(statements[fence])
    _expire(case)
    before = _rows(case, "turn_agent_execution")
    if not fence.startswith("buffer"):
        async with open_shared_ctx(case.fixture.db_path) as db:
            await db.execute("BEGIN")
            assert not await is_reference_recoverable_in_connection(db, turn_id="t_reference")
            await db.rollback()
    assert await case.store.recover_expired_chat_spawn_dispatches() == []
    assert _rows(case, "turn_agent_execution") == before
    assert _rows(case, "turn_events") == []


@pytest.mark.parametrize(
    "receiver_case",
    ["source", "source_mcp", "source_invoke", "source_other", "source_no_agent", "source_agent_binding", "source_agent_policy"],
    indirect=True,
)
async def test_recovery_classification_does_not_adopt_other_stored_execution_contracts(receiver_case, request):
    """General storage producers retain immutable guards; no mixed HTTP acceptance is claimed."""
    case = receiver_case.case
    deadline = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute(
            "INSERT INTO chat_turn_spawn_dispatch(turn_id,session_id,dispatch_kind,state,lease_expires_at,created_at,updated_at) "
            "VALUES('t_reference','s_reference','start','claimed',?,?,?)",
            (deadline, deadline, deadline),
        )
    supported = request.node.callspec.params["receiver_case"] in {"source", "source_mcp"}
    async with open_shared_ctx(case.fixture.db_path) as db:
        await db.execute("BEGIN")
        assert await is_reference_recoverable_in_connection(db, turn_id="t_reference") is supported
        assert not await is_reference_runtime_in_connection(db, turn_id="t_reference")
        await db.rollback()
    assert await case.store.recover_expired_chat_spawn_dispatches() == (["t_reference"] if supported else [])
    assert _rows(case, "turn_agent_execution")[0]["status"] == ("failed" if supported else "admitted")
    assert _rows(case, "chat_reference_resolutions") == _rows(case, "chat_reference_evidence") == []


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_concurrent_mixed_recovery_commits_one_terminal(receiver_case):
    case = receiver_case.case
    await candidate(receiver_case)
    await case.store.finalize_started_chat_submission(**case.args)
    _expire(case)
    results = []

    async def recover():
        results.append(await case.store.recover_expired_chat_spawn_dispatches())

    async with anyio.create_task_group() as group:
        group.start_soon(recover)
        group.start_soon(recover)
    assert sorted(results) == [[], ["t_reference"]]
    assert sum(row["event_type"] == "done" for row in _rows(case, "turn_events")) == 1
    assert _rows(case, "turn_agent_execution")[0]["status"] == "failed"
