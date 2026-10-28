"""Real admission + separate SQLite claimers; a pointer never proves a second win."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import aiosqlite
import bowhead_core.chat_reference_execution as execution
import pytest
from aiosqlite.context import contextmanager
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
from test_reference_primary_reader import accepted_case as _accepted_case_fixture
from test_reference_primary_reader import start_case as _start_case_fixture

accepted_case = _accepted_case_fixture
start_case = _start_case_fixture
pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _claim(case: SimpleNamespace, owner_id: str = "worker-a", **kwargs):
    return await execution.claim_reference_primary_for_worker(
        scope=case.scope,
        turn_id="t_reference",
        owner_id=owner_id,
        expected_generation=kwargs.pop("expected_generation", 1),
        bindings=AgentExecutionBindingStore(case.fixture.db_path),
        **kwargs,
    )


def _facts(case: SimpleNamespace):
    with sqlite3.connect(case.fixture.db_path) as db:
        return {
            table: db.execute(f"SELECT * FROM {table} WHERE turn_id='t_reference'").fetchall()
            for table in (
                "turn_agent_execution",
                "turn_agent_binding",
                "chat_turn_spawn_dispatch",
                "chat_reference_plans",
                "chat_submission_receipts",
                "turn_events",
                "chat_reference_resolutions",
                "chat_reference_evidence",
            )
        }


@pytest.mark.parametrize("second_owner", ["worker-a", "worker-b"])
async def test_competing_claimers_have_one_winner_and_one_exact_pointer(accepted_case: SimpleNamespace, second_owner: str) -> None:
    case = accepted_case
    before = _facts(case)
    results = await asyncio.gather(_claim(case), _claim(case, second_owner))
    assert sorted(result.outcome for result in results) == ["already_running", "claimed"]
    first, second = results
    assert first.primary == second.primary
    assert first.execution_generation == second.execution_generation == 1
    assert first.primary.binding.status == "running"
    with sqlite3.connect(case.fixture.db_path) as db:
        row = db.execute("SELECT status,started_at,id FROM turn_agent_execution WHERE turn_id='t_reference'").fetchone()
        assert row[0] == "running" and row[1] and row[2] == first.primary.binding.execution_id
        winner = next(i for i, result in enumerate(results) if result.outcome == "claimed")
        assert db.execute("SELECT owner_id,state FROM chat_turn_spawn_dispatch WHERE turn_id='t_reference'").fetchone() == (
            ["worker-a", second_owner][winner],
            "spawned",
        )
    after = _facts(case)
    assert len(after["turn_agent_execution"]) == len(after["chat_reference_resolutions"]) == 1
    assert after["chat_reference_evidence"] == []
    for table in before.keys() - {"turn_agent_execution", "chat_turn_spawn_dispatch", "chat_reference_resolutions"}:
        assert after[table] == before[table]
    # Repeated calls do not refresh the lease or execution timestamps.
    retry = await _claim(case, "worker-c", lease_seconds=300)
    assert retry.outcome == "already_running" and retry.primary == first.primary
    assert _facts(case) == after


@pytest.mark.parametrize(
    "denial",
    [
        "cancel",
        "expired",
        "naive_deadline",
        "generation",
        "lease_generation",
        "resume",
        "lease_kind",
        "state",
        "owned",
        "missing_dispatch",
        "dispatch_session",
        "inactive_session",
        "terminal_turn",
        "running_without_owner",
        "ended_execution",
        "assignment",
        "policy_epoch",
        "session_deleted",
        "parent",
        "turn_protocol",
        "session_protocol",
    ],
)
async def test_invalid_fence_or_authority_never_changes_reserved_facts(accepted_case: SimpleNamespace, denial: str) -> None:
    case = accepted_case
    with sqlite3.connect(case.fixture.db_path) as db:
        changes = {
            "cancel": "UPDATE chat_turn_spawn_dispatch SET cancel_requested_at='now'",
            "expired": "UPDATE chat_turn_spawn_dispatch SET lease_expires_at='2000-01-01T00:00:00+00:00'",
            "naive_deadline": "UPDATE chat_turn_spawn_dispatch SET lease_expires_at='2999-01-01T00:00:00'",
            "generation": "UPDATE turns SET execution_generation=2 WHERE id='t_reference'",
            "lease_generation": "UPDATE chat_turn_spawn_dispatch SET lease_execution_generation=2",
            "resume": "UPDATE chat_turn_spawn_dispatch SET dispatch_kind='follow_up'",
            "lease_kind": "UPDATE chat_turn_spawn_dispatch SET lease_kind='resume'",
            "state": "UPDATE chat_turn_spawn_dispatch SET state='failed'",
            "owned": "UPDATE chat_turn_spawn_dispatch SET owner_id='someone-else'",
            "missing_dispatch": "DELETE FROM chat_turn_spawn_dispatch",
            "dispatch_session": "UPDATE chat_turn_spawn_dispatch SET session_id='s_m3b'",
            "inactive_session": "UPDATE sessions SET active_turn_id=NULL WHERE id='s_reference'",
            "terminal_turn": "UPDATE turns SET status='completed' WHERE id='t_reference'",
            "running_without_owner": "UPDATE turn_agent_execution SET status='running',started_at='2026-09-10T00:00:00+00:00'",
            "ended_execution": "UPDATE turn_agent_execution SET status='blocked',ended_at='now',failure_code='test'",
            "assignment": "UPDATE agent_assignment SET access_state='revoked'",
            "session_deleted": "UPDATE sessions SET deleted_at='now' WHERE id='s_reference'",
            "parent": "UPDATE chat_turn_spawn_dispatch SET parent_turn_id='t_m3b'",
            # Deliberately inject inconsistent protocol metadata. Both cases
            # must be isolated, not treated as initial legacy execution.
            "turn_protocol": "UPDATE turns SET terminal_protocol_version=1 WHERE id='t_reference'",
            "session_protocol": "UPDATE sessions SET active_turn_protocol_version=1,active_turn_generation=1,active_turn_claim_epoch=1 WHERE id='s_reference'",
        }
        if denial == "policy_epoch":
            case.scope = case.scope.model_copy(update={"policy_epoch": case.scope.policy_epoch + 1})
        else:
            if denial == "turn_protocol":
                # Corruption injection only: normal writers cannot convert a
                # legacy Turn or leave an incomplete v1 admission envelope.
                db.execute("DROP TRIGGER turns_terminal_protocol_no_legacy_upgrade")
                db.execute("DROP TRIGGER turns_terminal_protocol_shape_update")
            elif denial == "session_protocol":
                # Likewise bypass the normal protocol-reference protection to
                # verify that a damaged active marker fails closed on read.
                db.execute("DROP TRIGGER sessions_terminal_active_reference_update")
            if denial == "terminal_turn":
                db.execute("DROP TRIGGER trg_reference_primary_completion")
            db.execute(changes[denial])
    before = _facts(case)
    with pytest.raises((ValueError, RuntimeError)):
        await _claim(case)
    assert _facts(case) == before


async def test_expected_generation_is_not_inferred_from_current_turn(accepted_case: SimpleNamespace) -> None:
    before = _facts(accepted_case)
    with pytest.raises(ValueError, match="reference_execution_fence_lost"):
        await _claim(accepted_case, expected_generation=2)
    assert _facts(accepted_case) == before


async def test_matching_later_generation_is_not_an_initial_start(accepted_case: SimpleNamespace) -> None:
    case = accepted_case
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("UPDATE turns SET execution_generation=2 WHERE id='t_reference'")
        db.execute("UPDATE chat_turn_spawn_dispatch SET lease_execution_generation=2")
    before = _facts(case)
    with pytest.raises(ValueError, match="reference_execution_fence_lost"):
        await _claim(case, expected_generation=2)
    assert _facts(case) == before


@pytest.mark.parametrize("change", ["cancel", "permission"])
async def test_duplicate_pointer_still_requires_current_authority_and_fence(accepted_case: SimpleNamespace, change: str) -> None:
    case = accepted_case
    await _claim(case)
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute(
            "UPDATE chat_turn_spawn_dispatch SET cancel_requested_at='now'"
            if change == "cancel"
            else "UPDATE agent_assignment SET access_state='revoked'"
        )
    before = _facts(case)
    with pytest.raises((ValueError, RuntimeError)):
        await _claim(case)
    assert _facts(case) == before


async def test_invalid_claim_arguments_are_rejected_before_opening_storage(accepted_case: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_open(*args, **kwargs):
        pytest.fail("invalid input opened storage")

    monkeypatch.setattr(execution, "open_shared_ctx", unexpected_open)
    for arguments in (
        {"owner_id": " "},
        {"owner_id": None},
        {"expected_generation": True},
        {"expected_generation": "1"},
        {"expected_generation": 0},
        {"lease_seconds": True},
        {"lease_seconds": 0},
        {"lease_seconds": 301},
    ):
        with pytest.raises(ValueError, match="reference_worker_claim_invalid"):
            await _claim(accepted_case, **arguments)


@pytest.mark.parametrize("after_claim", [False, True])
async def test_expiry_at_final_read_cannot_be_renewed(accepted_case: SimpleNamespace, after_claim: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    case = accepted_case
    if after_claim:
        await _claim(case)
    before = _facts(case)
    original = execution.read_reference_primary_in_connection
    clock = datetime.now(UTC)
    monkeypatch.setattr(execution, "_now", lambda: clock)

    async def read_then_expire(*args, **kwargs):
        nonlocal clock
        result = await original(*args, **kwargs)
        clock += timedelta(minutes=10)
        return result

    monkeypatch.setattr(execution, "read_reference_primary_in_connection", read_then_expire)
    with pytest.raises(ValueError, match="reference_execution_fence_lost"):
        await _claim(case)
    assert _facts(case) == before


@pytest.mark.parametrize("interruption", ["failure", "cancel", "cas_lost"])
async def test_partial_transition_rolls_back_both_rows(accepted_case: SimpleNamespace, interruption: str, monkeypatch: pytest.MonkeyPatch) -> None:
    case = accepted_case
    before = _facts(case)
    original = aiosqlite.Connection.execute

    @contextmanager
    async def interrupt(db, sql, parameters=None):
        if sql.startswith("UPDATE chat_turn_spawn_dispatch SET state='spawned'"):
            if interruption == "cas_lost":
                return SimpleNamespace(rowcount=0)
            # Interrupt after both writes reached SQLite, before commit.
            await original(db, sql, parameters)
            if interruption == "cancel":
                raise asyncio.CancelledError()
            raise sqlite3.OperationalError("injected storage failure")
        return await original(db, sql, parameters)

    monkeypatch.setattr(aiosqlite.Connection, "execute", interrupt)
    error = {"failure": sqlite3.OperationalError, "cancel": asyncio.CancelledError, "cas_lost": ValueError}[interruption]
    with pytest.raises(error):
        await _claim(case)
    assert _facts(case) == before
    monkeypatch.setattr(aiosqlite.Connection, "execute", original)
    assert (await _claim(case)).outcome == "claimed"


async def test_lost_commit_ack_does_not_issue_another_start(accepted_case: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    original = aiosqlite.Connection.commit

    async def commit_then_lose_ack(db):
        await original(db)
        raise sqlite3.OperationalError("commit acknowledgement lost")

    monkeypatch.setattr(aiosqlite.Connection, "commit", commit_then_lose_ack)
    with pytest.raises(sqlite3.OperationalError):
        await _claim(accepted_case)
    before = _facts(accepted_case)
    monkeypatch.setattr(aiosqlite.Connection, "commit", original)
    assert (await _claim(accepted_case)).outcome == "already_running"
    assert _facts(accepted_case) == before


@pytest.mark.parametrize("change", ["cancel", "permission", "expiry"])
async def test_claim_rechecks_after_waiting_for_the_write_lock(accepted_case: SimpleNamespace, change: str, monkeypatch: pytest.MonkeyPatch) -> None:
    case = accepted_case
    original = aiosqlite.Connection.execute
    waiting = asyncio.Event()
    clock = datetime.now(UTC)
    monkeypatch.setattr(execution, "_now", lambda: clock)

    @contextmanager
    async def observe_begin(db, sql, parameters=None):
        if sql == "BEGIN IMMEDIATE":
            waiting.set()
        return await original(db, sql, parameters)

    async with open_shared_ctx(str(case.fixture.db_path)) as blocker:
        await blocker.execute("BEGIN IMMEDIATE")
        monkeypatch.setattr(aiosqlite.Connection, "execute", observe_begin)
        task = asyncio.create_task(_claim(case))
        try:
            await asyncio.wait_for(waiting.wait(), timeout=5)
            if change == "cancel":
                await blocker.execute("UPDATE chat_turn_spawn_dispatch SET cancel_requested_at='now'")
            elif change == "permission":
                await blocker.execute("UPDATE agent_assignment SET access_state='revoked'")
            else:
                clock += timedelta(minutes=10)
            await blocker.commit()
            before = _facts(case)
            with pytest.raises((ValueError, RuntimeError)):
                await asyncio.wait_for(task, timeout=5)
            assert _facts(case) == before
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
