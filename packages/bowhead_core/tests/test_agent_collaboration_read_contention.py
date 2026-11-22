"""Collaboration reads coexist with authority writers and retain current access checks."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core._db import close_pooled_readers, open_shared_ctx
from bowhead_core.agent_collaboration import CollaborationStore, CollaborationUnavailable
from bowhead_core.agent_collaboration_runtime import CollaborationRuntime
from test_agent_collaboration import CollaborationFixture
from test_agent_collaboration import collab as collab

from bowhead_core import _db, sqlite_capacity, sqlite_domain_runtime

pytestmark = pytest.mark.integration


@pytest.fixture
async def split_collab(collab: CollaborationFixture) -> AsyncIterator[CollaborationFixture]:
    await close_pooled_readers()
    baseline = sqlite_capacity._used
    await asyncio.to_thread(upgrade, f"sqlite:///{collab.path}")
    collab.store = CollaborationStore(collab.path, cipher=collab.store.cipher, clock=lambda: collab.clock[0])
    try:
        yield collab
    finally:
        await close_pooled_readers()
        assert sqlite_capacity._used == baseline, "Collaboration reads must release their physical connections after pool cleanup"


async def read_collaboration(fixture: CollaborationFixture, method: str, question: dict) -> dict | list[dict] | None:
    arguments = {}
    if method in {"roster", "inbox"}:
        arguments["execution_id"] = fixture.recipient
    if method in {"roster", "inbox", "read", "projection"}:
        arguments["authorize"] = fixture.authorize
    if method == "read":
        arguments["operation_id"] = question["operation_id"]
    return await getattr(fixture.store, method)(fixture.worker, **arguments)


@pytest.mark.parametrize("method", ["admission", "roster", "inbox", "read", "projection", "pending_publications"])
async def test_reads_coexist_with_revocation_and_observe_its_commit(split_collab: CollaborationFixture, method: str) -> None:
    fixture = split_collab
    question = await fixture.ask(required=False)
    expected = await read_collaboration(fixture, method, question)
    assert expected
    async with open_shared_ctx(str(fixture.path)) as writer:
        await writer.execute("BEGIN IMMEDIATE")
        try:
            await writer.execute(
                "DELETE FROM workspace_members WHERE workspace_id=? AND user_id=?",
                (fixture.worker.workspace_id, fixture.worker.user_id),
            )
            assert await asyncio.wait_for(read_collaboration(fixture, method, question), timeout=2) == expected
            await writer.commit()
        finally:
            await writer.rollback()
    with pytest.raises(ValueError, match="reference_scope_unavailable"):
        await read_collaboration(fixture, method, question)
    with pytest.raises(CollaborationUnavailable, match="collaboration_worker_unavailable"):
        await fixture.ask(identity=fixture.identity(step="after-revocation"))
    assert await fixture.count() == 1


async def test_cancelled_inbox_read_preserves_delivery_cursor_and_write_fence(split_collab: CollaborationFixture) -> None:
    fixture = split_collab
    question = await fixture.ask(recipient_id=fixture.lead, required=False)
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def paused_authorize(db, worker, sender, recipient, content) -> None:
        await fixture.authorize(db, worker, sender, recipient, content)
        if content["body"] == "Which refund definition applies?":
            entered.set()
            await blocked.wait()

    runtime = CollaborationRuntime(fixture.store, fixture.worker, authorize=paused_authorize)
    reading = asyncio.create_task(runtime.context(fixture.lead))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        async with open_shared_ctx(str(fixture.path)) as authority_writer:
            await authority_writer.execute(
                "INSERT INTO access_policy_state(team_id,epoch,updated_at) VALUES(?,1,?) "
                "ON CONFLICT(team_id) DO UPDATE SET epoch=epoch+1,updated_at=excluded.updated_at",
                (fixture.worker.team_id, "2026-11-07T00:00:00+00:00"),
            )
            await authority_writer.commit()
        async with open_shared_ctx(fixture.store.db_path) as writer:
            await writer.execute(
                "UPDATE chat_turn_spawn_dispatch SET cancel_requested_at=? WHERE turn_id=?",
                ("2026-11-07T00:00:00+00:00", fixture.worker.turn_id),
            )
            await writer.commit()
        reading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reading
    finally:
        reading.cancel()
        await asyncio.gather(reading, return_exceptions=True)
    runtime.authorize = fixture.authorize
    context = await runtime.context(fixture.lead)
    assert [item["operation_id"] for item in context["items"]] == [question["operation_id"]]
    assert runtime._cursors == {}
    with pytest.raises(CollaborationUnavailable, match="collaboration_worker_unavailable"):
        await fixture.store.complete(fixture.worker, execution_id=fixture.lead)
    async with open_shared_ctx(str(fixture.path)) as writer:
        await writer.execute(
            "DELETE FROM workspace_members WHERE workspace_id=? AND user_id=?",
            (fixture.worker.workspace_id, fixture.worker.user_id),
        )
        await writer.commit()
    with pytest.raises(ValueError, match="reference_scope_unavailable"):
        await runtime.context(fixture.lead)
    assert await fixture.count() == 1


async def test_completion_checks_run_beside_a_core_authority_writer_and_keep_the_worker_fence(split_collab: CollaborationFixture) -> None:
    """116 1.7.49 the regression case: a helper's completion check failed at once with "SQLite authority
    lease is busy" while a Space metadata save held the exclusive core lease, and the Auto plan ended
    blocked. Checking for expired questions and reading obligations writes nothing, so neither may
    need a writer transaction or the shared core lease."""
    fixture = split_collab
    question = await fixture.ask()
    async with open_shared_ctx(str(fixture.path)) as writer:
        await writer.execute("BEGIN IMMEDIATE")
        try:
            # A Space archive changes the fenced status, so it holds the exclusive core lease. (The 116 case
            # was a Space metadata save; 0230 stopped fencing the revision and description it changes.)
            await writer.execute("UPDATE workspaces SET status='archived' WHERE id=?", (fixture.worker.workspace_id,))
            # Guard against a holder that stops fencing: the exclusive core lease must really be held.
            assert writer._conn._leases.get("core") == 2, writer._conn._leases
            assert await asyncio.wait_for(fixture.store.expire(fixture.worker), timeout=2) == []
            obligations = await asyncio.wait_for(fixture.store.obligations(fixture.worker, execution_id=fixture.requester), timeout=2)
            assert [item["question_id"] for item in obligations] == [question["operation_id"]]
        finally:
            await writer.rollback()
    # A due question still closes in its write transaction.
    fixture.clock[0] += 61
    assert len(await fixture.store.expire(fixture.worker)) == 1
    assert await fixture.store.obligations(fixture.worker, execution_id=fixture.requester) == []
    async with open_shared_ctx(fixture.store.db_path) as writer:
        await writer.execute(
            "UPDATE chat_turn_spawn_dispatch SET cancel_requested_at=? WHERE turn_id=?",
            ("2026-11-17T00:00:00+00:00", fixture.worker.turn_id),
        )
        await writer.commit()
    for check in (fixture.store.expire(fixture.worker), fixture.store.obligations(fixture.worker, execution_id=fixture.requester)):
        with pytest.raises(CollaborationUnavailable, match="collaboration_worker_unavailable"):
            await check


async def _hold_exclusive_core_lease(fixture: CollaborationFixture, released: asyncio.Event, seconds: float) -> None:
    """Hold the exclusive core lease the way a Space archive does, then roll back.

    Space metadata saves no longer fence (revision 0230), so the holder changes the fenced status.
    """
    async with open_shared_ctx(str(fixture.path)) as writer:
        await writer.execute("BEGIN IMMEDIATE")
        try:
            await writer.execute("UPDATE workspaces SET status='archived' WHERE id=?", (fixture.worker.workspace_id,))
            # Guard against a holder that stops fencing: the exclusive core lease must really be held.
            assert writer._conn._leases.get("core") == 2, writer._conn._leases
            released.set()
            await asyncio.sleep(seconds)
        finally:
            await writer.rollback()


async def test_collaboration_writes_repeat_until_an_authority_writer_lets_go(split_collab: CollaborationFixture) -> None:
    """116 1.7.49: a collaboration write pinned the shared core lease at its first scope read and failed
    at once beside an authority writer. Writes now repeat their whole transaction and land once the lease is free."""
    fixture = split_collab
    holding = asyncio.Event()
    holder = asyncio.create_task(_hold_exclusive_core_lease(fixture, holding, 0.3))
    await holding.wait()
    question = await asyncio.wait_for(fixture.ask(), timeout=10)
    await holder
    assert await fixture.count() == 1

    holding = asyncio.Event()
    holder = asyncio.create_task(_hold_exclusive_core_lease(fixture, holding, 0.3))
    await holding.wait()
    await asyncio.wait_for(fixture.reply(question), timeout=10)
    await holder
    assert await fixture.count() == 2


async def test_a_collaboration_write_that_never_gets_the_lease_fails_as_before_and_writes_nothing(
    split_collab: CollaborationFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = split_collab
    monkeypatch.setattr(_db, "AUTHORITY_BUSY_WAIT_SECONDS", 0.2)
    # Each attempt may also wait inside its transaction; keep that shorter than the holder too.
    monkeypatch.setattr(sqlite_domain_runtime, "IN_TRANSACTION_LEASE_WAIT_SECONDS", 0.05)
    holding = asyncio.Event()
    holder = asyncio.create_task(_hold_exclusive_core_lease(fixture, holding, 1.0))
    await holding.wait()
    try:
        with pytest.raises(sqlite3.OperationalError, match="authority lease is busy"):
            await fixture.ask()
    finally:
        await holder
    assert await fixture.count() == 0
