"""Automatic reservations preserve user intent and settle without a worker."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_execution import read_reference_skill_primary_in_connection
from bowhead_core.chat_reference_scope import require_reference_request_scope
from bowhead_core.chat_reference_storage import ReferenceScope
from test_reference_primary_runtime import _expire, _rows
from test_reference_receiver_candidates import discovery
from test_reference_receiver_choice import chosen
from test_reference_start_admission import _assert_no_acceptance
from test_skill_read_receiver_integration import receiver_case as _receiver_case
from test_skill_read_receiver_integration import start_case as _start_case

receiver_case, start_case = _receiver_case, _start_case
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission_optional_unused_sop"], indirect=True),
]


async def prepare_auto(fixture):
    read, kwargs, _ = await discovery(fixture)
    result = await chosen(fixture, await read(), kwargs["input_document"])
    fixture.case.args.update(reference_start=result, user_text=result.input_document.display_text())
    return result


async def read_auto(fixture):
    request_scope = fixture.scope.model_copy(update={"session_id": "s_reference"})
    async with open_shared_ctx(fixture.case.fixture.db_path) as db:
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        changes = db.total_changes  # Connection-wide cumulative count; compare this transaction's delta on reused connections.
        try:
            epoch = await require_reference_request_scope(db, request_scope)
            result = await read_reference_skill_primary_in_connection(
                db,
                scope=ReferenceScope(**request_scope.model_dump(), policy_epoch=epoch),
                turn_id="t_reference",
                bindings=fixture.case.bindings,
            )
            assert db.total_changes == changes and db.in_transaction
            return result
        finally:
            await db.rollback()


async def test_concurrent_finalize_keeps_one_primary_and_original_source(receiver_case):
    fixture, case = receiver_case, receiver_case.case
    admission = await prepare_auto(fixture)
    receipts = await asyncio.gather(*(case.store.finalize_started_chat_submission(**case.args) for _ in range(2)))
    assert receipts[0].response == receipts[1].response
    assert len(_rows(case, "turn_agent_execution")) == len(_rows(case, "chat_submission_receipts")) == 1
    primary = await read_auto(fixture)
    assert primary.source.input_document == admission.input_document
    assert primary.binding.execution_id == _rows(case, "turn_agent_binding")[0]["primary_execution_id"]
    assert primary.binding.revision_ref == admission.automatic_receiver.version
    assert len(primary.plan.admissions) == 1 and primary.plan.admissions[0].operation == "read"


@pytest.mark.parametrize("failure", ["root", "receipt", "migration", "revoked"])
async def test_failed_automatic_acceptance_leaves_no_partial_turn(receiver_case, failure):
    fixture, case = receiver_case, receiver_case.case
    await prepare_auto(fixture)
    with sqlite3.connect(case.fixture.db_path) as db:
        if failure in {"root", "receipt"}:
            table = "turn_agent_binding" if failure == "root" else "chat_submission_receipts"
            db.execute(f"CREATE TRIGGER fail_auto BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'injected automatic failure'); END")
        elif failure == "migration":
            db.execute("DROP TRIGGER trg_reference_automatic_receiver_source")
        else:
            db.execute("UPDATE agent_assignment SET access_state='revoked'")
    with pytest.raises((ValueError, RuntimeError, sqlite3.IntegrityError)):
        await case.store.finalize_started_chat_submission(**case.args)
    _assert_no_acceptance(case)


@pytest.mark.parametrize("state", ["expired", "cancelled", "revoked"])
async def test_automatic_reservation_recovers_once_without_dispatch(receiver_case, state):
    fixture, case = receiver_case, receiver_case.case
    await prepare_auto(fixture)
    receipt = await case.store.finalize_started_chat_submission(**case.args)
    source = _rows(case, "chat_turn_user_sources")
    execution = _rows(case, "turn_agent_execution")[0]
    if state == "cancelled":
        await case.store.request_cancel_turn(
            team_id=fixture.scope.team_id,
            user_id=fixture.scope.user_id,
            workspace_id=fixture.scope.workspace_id,
            turn_id="t_reference",
            expected_execution_generation=1,
            cancel_request_id="cancel-auto",
        )
    if state == "revoked":
        with sqlite3.connect(case.fixture.db_path) as db:
            db.execute("UPDATE agent_assignment SET access_state='revoked'")
        with pytest.raises((ValueError, RuntimeError)):
            await read_auto(fixture)
    _expire(case)
    assert await case.store.recover_expired_chat_spawn_dispatches() == ["t_reference"]
    assert await case.store.recover_expired_chat_spawn_dispatches() == []
    status = "cancelled" if state == "cancelled" else "failed"
    current = _rows(case, "turn_agent_execution")
    assert len(current) == 1 and current[0]["id"] == execution["id"] and current[0]["status"] == status
    assert _rows(case, "chat_turn_user_sources") == source
    assert (
        _rows(case, "chat_reference_resolutions") == _rows(case, "chat_reference_content_snapshots") == _rows(case, "chat_reference_evidence") == []
    )
    terminals = [json.loads(row["payload_json"]) for row in _rows(case, "turn_events") if row["event_type"] == "done"]
    assert len(terminals) == 1 and terminals[0]["status"] == status
    assert json.loads(_rows(case, "chat_submission_receipts")[0]["response_json"]) == receipt.response


async def test_failed_automatic_recovery_can_retry_the_same_execution(receiver_case):
    fixture, case = receiver_case, receiver_case.case
    await prepare_auto(fixture)
    await case.store.finalize_started_chat_submission(**case.args)
    _expire(case)
    before = {t: _rows(case, t) for t in ("turn_agent_execution", "chat_turn_spawn_dispatch", "turn_events")}
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("CREATE TRIGGER fail_auto_done BEFORE INSERT ON turn_events WHEN NEW.event_type='done' BEGIN SELECT RAISE(ABORT,'injected'); END")
    assert await case.store.recover_expired_chat_spawn_dispatches() == []
    assert {t: _rows(case, t) for t in before} == before
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("DROP TRIGGER fail_auto_done")
    assert await case.store.recover_expired_chat_spawn_dispatches() == ["t_reference"]


async def test_database_rejects_automatic_root_without_its_execution(receiver_case):
    case = receiver_case.case
    await prepare_auto(receiver_case)
    await case.store.finalize_started_chat_submission(**case.args)
    with sqlite3.connect(case.fixture.db_path) as db:
        db.row_factory = sqlite3.Row
        root = dict(db.execute("SELECT * FROM turn_agent_binding WHERE turn_id='t_reference'").fetchone())
        db.execute("BEGIN")
        db.execute("DELETE FROM turn_agent_binding WHERE turn_id='t_reference'")
        root["primary_execution_id"] = None
        with pytest.raises(sqlite3.IntegrityError, match="automatic reference receiver source mismatch"):
            db.execute(f"INSERT INTO turn_agent_binding({','.join(root)}) VALUES({','.join('?' for _ in root)})", tuple(root.values()))
        db.rollback()
    assert len(_rows(case, "turn_agent_binding")) == 1
